"""Proxy pool: load a proxy list from a file and rotate through it.

Design goals
────────────
* No third-party imports — pure stdlib so it is trivially unit-testable
  and cannot break the container build.
* Tolerant parsing: accepts ``http://``, ``https://``, ``socks4://``,
  ``socks5://`` (and bare ``host:port`` → treated as ``http``). Blank lines
  and ``#`` comments are ignored, duplicates removed.
* Temporary cooldown: a proxy that fails (dead / blocked) is parked for
  ``cooldown`` seconds instead of being retried immediately. When every
  proxy is on cooldown the pool clears the cooldowns and reuses them —
  a flaky proxy still beats giving up.

The pool is deliberately simple and synchronous; callers pick a proxy with
``get()``, then report the outcome with ``mark_good()`` / ``mark_bad()``.
"""
from __future__ import annotations

import logging
import random
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_ALLOWED_SCHEMES = (
    "http://",
    "https://",
    "socks5://",
    "socks5h://",
)


def _normalize(line: str) -> str | None:
    """Return a normalized proxy URL, or None if the line is not a proxy."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None

    if line.lower().startswith(_ALLOWED_SCHEMES):
        return line

    # Bare "host:port" (optionally "user:pass@host:port") → assume http.
    if "//" not in line and ":" in line:
        return "http://" + line

    return None


def load_proxies(path: str) -> list[str]:
    """Read *path* and return a de-duplicated list of proxy URLs.

    Returns an empty list (and logs a warning) when the path is empty or the
    file does not exist, so the caller can transparently fall back to a
    direct connection.
    """
    if not path:
        return []

    p = Path(path)
    if not p.is_file():
        logger.warning("Proxy file not found or not a file: %s", path)
        return []

    proxies: list[str] = []
    seen: set[str] = set()

    for raw in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        norm = _normalize(raw)
        if norm and norm not in seen:
            seen.add(norm)
            proxies.append(norm)

    logger.info("Loaded %d proxies from %s", len(proxies), path)
    return proxies


def mask(proxy: str | None) -> str:
    """Return a log-safe rendering of a proxy (hide any user:pass@ credentials)."""
    if not proxy:
        return "direct"
    if "@" in proxy:
        scheme, _, rest = proxy.partition("://")
        host = rest.split("@", 1)[1] if "@" in rest else rest
        return f"{scheme}://***@{host}"
    return proxy


class ProxyPool:
    """A rotating pool of proxy URLs with per-proxy failure cooldown."""

    def __init__(self, proxies: list[str], cooldown: float = 300.0) -> None:
        self._proxies: list[str] = list(dict.fromkeys(proxies))  # keep order, unique
        self._cooldown: float = max(0.0, cooldown)
        self._bad: dict[str, float] = {}  # proxy -> monotonic time it may retry

    def __len__(self) -> int:
        return len(self._proxies)

    @property
    def empty(self) -> bool:
        return not self._proxies

    def _available(self) -> list[str]:
        now = time.monotonic()
        return [p for p in self._proxies if self._bad.get(p, 0.0) <= now]

    def get(self) -> str | None:
        """Return a random currently-usable proxy, or None if the pool is empty."""
        if not self._proxies:
            return None

        available = self._available()
        if not available:
            # Everything is cooling down — reset and reuse rather than fail.
            logger.debug("All proxies on cooldown; resetting cooldowns.")
            self._bad.clear()
            available = list(self._proxies)

        return random.choice(available)

    def mark_bad(self, proxy: str | None) -> None:
        """Park a failing proxy for the cooldown window."""
        if proxy:
            self._bad[proxy] = time.monotonic() + self._cooldown

    def mark_good(self, proxy: str | None) -> None:
        """Clear any cooldown on a proxy that just succeeded."""
        if proxy:
            self._bad.pop(proxy, None)

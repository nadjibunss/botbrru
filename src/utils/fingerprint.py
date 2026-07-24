"""
src/services/fingerprint.py
Manages Shopee risktoken / SECSDK handling.

Shopee's SECSDK is a heavily obfuscated JavaScript library that generates a
signed token (x-sz-secsdk-token) tied to the browser environment.  It cannot
be replicated in pure Python.  Instead this module handles tokens that the
user extracts manually from their browser (DevTools → Network tab →
x-sz-secsdk-token request header) and validates their shape and context.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Plausible risktoken: base64url / base64 printable characters, 100–3000 chars.
_RISKTOKEN_RE = re.compile(r"^[A-Za-z0-9_\-=.:|+/]{20,5000}$")

# Keywords that suggest the API response is demanding a fresh token.
_CAPTCHA_KEYWORDS = frozenset({"risktoken", "secsdk", "captcha"})


@dataclass(slots=True)
class RiskToken:
    """
    Represents a validated Shopee SECSDK / risktoken string.

    Attributes:
        raw:    The raw token string as extracted from the browser.
        length: Pre-computed string length for quick logging/debugging.
    """

    raw: str
    length: int

    def is_plausible(self) -> bool:
        """
        Return True if the token matches the expected character set and length.

        This is a *syntactic* check only — it cannot verify that the token
        is cryptographically valid for the current Shopee session.
        """
        return bool(_RISKTOKEN_RE.match(self.raw))


def parse_risktoken(raw: str | None) -> RiskToken | None:
    """
    Parse and lightly validate a raw risktoken string.

    Args:
        raw: The raw string from the user or database.  May be ``None`` or empty.

    Returns:
        A :class:`RiskToken` if the input is non-empty and passes the shape
        check, otherwise ``None``.
    """
    if not raw or not raw.strip():
        return None

    token = RiskToken(raw=raw.strip(), length=len(raw.strip()))

    if not token.is_plausible():
        logger.warning(
            "Risktoken shape check failed (len=%d); token will be ignored.",
            token.length,
        )
        return None

    logger.debug("Parsed risktoken OK (len=%d)", token.length)
    return token


def risktoken_expired_signals(*, status: int, body: dict | None) -> bool:
    """
    Heuristically decide whether an API response signals that the risktoken
    has expired or is being demanded by Shopee.

    Decision criteria (any one is sufficient):

    * ``body.error == 90309999`` — Shopee's canonical anti-bot error code.
    * ``error_msg`` or ``message`` in body contains "risktoken", "secsdk", or
      "captcha" (case-insensitive).
    * HTTP 403 and ``is_login == False`` in the body (session/token mismatch).

    Args:
        status: HTTP response status code.
        body:   Parsed JSON response body, or ``None`` on decode failure.

    Returns:
        ``True`` if Shopee appears to be demanding a fresh risktoken.
    """
    if body is None:
        return False

    # Explicit anti-bot error code
    if body.get("error") == 90309999:
        logger.debug("risktoken_expired_signals: error 90309999 detected")
        return True

    # Text-based signal in error message fields
    for field in ("error_msg", "message"):
        text: str = str(body.get(field) or "").lower()
        if any(kw in text for kw in _CAPTCHA_KEYWORDS):
            logger.debug(
                "risktoken_expired_signals: captcha keyword in field '%s': %r",
                field,
                text[:120],
            )
            return True

    # 403 with an explicit not-logged-in flag
    if status == 403 and body.get("is_login") is False:
        logger.debug("risktoken_expired_signals: HTTP 403 + is_login=False")
        return True

    return False

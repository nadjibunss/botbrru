"""Standalone tests for src/utils/proxy.py — no third-party deps, run with:

    python3 tests/test_proxy.py
"""
import importlib.util
import pathlib
import sys
import tempfile

MOD = pathlib.Path(__file__).resolve().parents[1] / "src" / "utils" / "proxy.py"
spec = importlib.util.spec_from_file_location("proxy", MOD)
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)

_failed = False


def check(name: str, cond: bool) -> None:
    global _failed
    print(("PASS" if cond else "FAIL"), "-", name)
    if not cond:
        _failed = True


# ── load_proxies ──────────────────────────────────────────────────────────
with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
    f.write(
        "# comment line\n"
        "\n"
        "http://1.1.1.1:80\n"
        "socks5://2.2.2.2:1080\n"
        "3.3.3.3:8080\n"          # bare host:port -> http
        "http://1.1.1.1:80\n"     # duplicate
        "   \n"
        "notaproxy\n"             # junk -> skipped
    )
    path = f.name

loaded = proxy.load_proxies(path)
check(
    "load: normalize + dedupe + skip junk",
    loaded == ["http://1.1.1.1:80", "socks5://2.2.2.2:1080", "http://3.3.3.3:8080"],
)
check("load: missing file -> []", proxy.load_proxies("/no/such/file.txt") == [])
check("load: empty path -> []", proxy.load_proxies("") == [])

# ── mask ──────────────────────────────────────────────────────────────────
check("mask: None -> direct", proxy.mask(None) == "direct")
check("mask: no creds passthrough", proxy.mask("http://1.2.3.4:80") == "http://1.2.3.4:80")
check("mask: hides credentials", proxy.mask("http://user:pass@1.2.3.4:80") == "http://***@1.2.3.4:80")

# ── ProxyPool ─────────────────────────────────────────────────────────────
pool = proxy.ProxyPool(["http://a:1", "http://b:2", "http://a:1"])  # dup collapses
check("pool: unique length", len(pool) == 2)
check("pool: not empty", pool.empty is False)
check("pool: get returns a member", pool.get() in {"http://a:1", "http://b:2"})

empty = proxy.ProxyPool([])
check("pool: empty length 0", len(empty) == 0 and empty.empty is True)
check("pool: empty get -> None", empty.get() is None)

all_bad = proxy.ProxyPool(["http://a:1", "http://b:2"], cooldown=100)
all_bad.mark_bad("http://a:1")
all_bad.mark_bad("http://b:2")
check("pool: all-bad resets and still yields", all_bad.get() in {"http://a:1", "http://b:2"})

one_bad = proxy.ProxyPool(["http://a:1", "http://b:2"], cooldown=100)
one_bad.mark_bad("http://a:1")
avail = one_bad._available()
check("pool: bad proxy excluded while others usable", "http://a:1" not in avail and "http://b:2" in avail)
one_bad.mark_good("http://a:1")
check("pool: mark_good re-enables", "http://a:1" in one_bad._available())

print("\nRESULT:", "FAIL" if _failed else "ALL PASS")
sys.exit(1 if _failed else 0)

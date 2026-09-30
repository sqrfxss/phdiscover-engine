"""Prove retry and cache behave as designed, without hitting the network."""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from phdiscover.cache import PositionCache
from phdiscover.reliability import RetryPolicy, is_transient, retry_async

CACHE = ROOT / "data" / "cache" / "test_cache.json"


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label:52} {got!r}")
    return ok


def main():
    results = []
    print("=== transient vs permanent classification ===")
    cases = [
        ("ConnectError: [Errno 11001] getaddrinfo failed", False),
        ("net::ERR_CERT_COMMON_NAME_INVALID", False),
        ("Page.goto: Timeout 60000ms exceeded.", True),
        ("net::ERR_TIMED_OUT at https://x", True),
        ("HTTP 403 (secure)", False),
        ("ReadError: ", True),
        ("SSL: SSLV3_ALERT_HANDSHAKE_FAILURE", True),
        ("HTTP 200 0 postings", False),
    ]
    for text, want in cases:
        results.append(check(f"is_transient({text[:34]})", is_transient(text), want))

    print("\n=== retry recovers from a transient failure ===")

    async def flaky():
        state = {"n": 0}

        async def fn():
            state["n"] += 1
            if state["n"] < 2:
                raise TimeoutError("net::ERR_TIMED_OUT")
            return (["position"], "200 1 postings")

        val, note = await retry_async(fn, policy=RetryPolicy(attempts=3, base_delay=0.01))
        return state["n"], val, note

    n, val, note = asyncio.run(flaky())
    results.append(check("attempts needed", n, 2))
    results.append(check("value recovered", val, ["position"]))
    results.append(check("note preserved", note, "200 1 postings"))

    print("\n=== retry gives up immediately on a permanent failure ===")

    async def blocked():
        calls = {"n": 0}

        async def fn():
            calls["n"] += 1
            raise RuntimeError("HTTP 403 Forbidden")

        await retry_async(fn, policy=RetryPolicy(attempts=3, base_delay=0.01))
        return calls["n"]

    results.append(check("attempts on a 403", asyncio.run(blocked()), 1))

    print("\n=== a failed source keeps its cached positions ===")
    CACHE.unlink(missing_ok=True)
    cache = PositionCache(CACHE)
    cache.record_success("goodboard", [
        {"url": "https://x/1", "title": "PhD A", "context": "ctx", "source": "goodboard"},
        {"url": "https://x/2", "title": "PhD B", "context": "ctx", "source": "goodboard"},
    ])
    cache.save()

    per_source = {
        "goodboard": {"n": 0, "method": "browser", "note": "net::ERR_TIMED_OUT"},
        "other": {"n": 3, "method": "http", "note": "200 3 postings"},
    }
    fresh = [{"url": "https://y/1", "title": "Fresh", "context": "c", "source": "other"}]
    merged, notes = cache.merge_into(fresh, per_source)

    results.append(check("cached positions survive", len(merged), 3))
    results.append(check("cache note recorded", len(notes), 1))
    results.append(check("source named in note", notes[0]["source"], "goodboard"))
    results.append(check("cached count in note", notes[0]["cached_positions"], 2))
    results.append(check("merge marks cache rows",
                         sum(1 for m in merged if m.get("from_cache")), 2))

    print("\n=== a successful source replaces its cache ===")
    per_source2 = {"goodboard": {"n": 1, "method": "browser", "note": "200 1 postings"}}
    fresh2 = [{"url": "https://z/1", "title": "New", "context": "c", "source": "goodboard"}]
    merged2, notes2 = cache.merge_into(fresh2, per_source2)
    results.append(check("no stale rows kept", len(merged2), 1))
    results.append(check("no cache notes", len(notes2), 0))

    print("\n=== a source that never worked is remembered as such ===")
    CACHE.unlink(missing_ok=True)
    c3 = PositionCache(CACHE)
    c3.record_failure("neverworked", "HTTP 403")
    results.append(check("tracked", "neverworked" in c3.data["sources"], True))
    results.append(check("no positions stored", c3.get("neverworked")[0], []))
    results.append(check("listed as never succeeded",
                         c3.stats()["sources_never_succeeded"], ["neverworked"]))

    CACHE.unlink(missing_ok=True)
    passed = sum(results)
    print(f"\n{'='*60}\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())

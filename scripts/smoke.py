"""Request every public route the way a visitor does, against production.

WHY THIS EXISTS. Twice in one day the site was announced as working when the
route a visitor would actually use returned an error: `/verify` 404'd after
being advertised, and every pasted CA except one reached Vercel's own
"The page could not be found". Both times the local test suite was green. A
green suite says the renderer is correct; it says nothing about what the CDN
serves, which is the only thing a person telling their followers to go and
check can afford to be wrong about.

So this is not a unit test. It talks to the live host, follows what the
browser follows, and asserts on the bytes that come back. Run it BEFORE
saying anything is live, and before handing over any copy that points at a
URL.

    python scripts/smoke.py                       # production
    python scripts/smoke.py --base http://...     # a preview deployment

Exit status is 0 only when every check passes.
"""

from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.request

BASE = "https://charlieprotocol.fun"
MEASURED = "8FhAXv2tfXUpyMbJsHDHX9zfiEb9PERzFWSY9sgLpump"

# A syntactically valid mainnet CA that this site has NOT measured. The point
# of the check is the miss, so it must never be a coin we might later measure
# into a hit -- an unmeasured coin is the common case under the submit model
# and its page is the one a stranger is most likely to see.
UNMEASURED = "A8C3xuqscfmyLrte3VmTqrAq8kgMASius9AFNANwpump"


def fetch(url: str) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "charlie-smoke/1"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    base = ap.parse_args().base.rstrip("/")

    # (path, expected status, substrings that MUST appear, substrings that must NOT)
    checks = [
        ("/", 200, ["Charlie Protocol"], ["NOT_FOUND"]),
        ("/verify", 200, ["Verify a coin", 'name="mint"'], ["NOT_FOUND"]),
        ("/coins", 200, ["coin observed"], ["NOT_FOUND"]),
        (f"/verify?mint={MEASURED}", 200, [MEASURED, "Results"], ["NOT_FOUND"]),
        (f"/verify/{MEASURED}", 200, [MEASURED], ["NOT_FOUND"]),
        (f"/coin/{MEASURED}", 200, [MEASURED], ["NOT_FOUND"]),
        (f"/coin/{MEASURED}.json", 200, ['"mint"'], ["NOT_FOUND"]),
        # The miss. 404 is the correct STATUS -- what matters is that the body
        # is our page and not Vercel's, which is exactly what shipped broken.
        (f"/verify?mint={UNMEASURED}", 404,
         ["No page for that coin yet", "Submit the CA"],
         ["The page could not be found"]),
        ("/assets/charlie.png", 200, [], []),
        ("/assets/charlie-scanning.gif", 200, [], []),
        ("/assets/charlie-found.gif", 200, [], []),
        ("/assets/incinerator-stack.png", 200, [], []),
        ("/assets/incinerator-smoke.png", 200, [], []),
    ]

    failures = []
    for path, want_status, must, must_not in checks:
        status, body = fetch(base + path)
        problems = []
        if status != want_status:
            problems.append(f"status {status}, wanted {want_status}")
        for token in must:
            if token not in body:
                problems.append(f"missing {token!r}")
        for token in must_not:
            if token in body:
                problems.append(f"contains {token!r}")
        mark = "FAIL" if problems else "ok"
        print(f"[{mark:>4}] {path}")
        for p in problems:
            print(f"         {p}")
        if problems:
            failures.append(path)

    print()
    if failures:
        print(f"{len(failures)} route(s) failed: {', '.join(failures)}")
        print("DO NOT tell anyone this is live.")
        return 1
    print(f"all {len(checks)} routes served correctly by {base}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

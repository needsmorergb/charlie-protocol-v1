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
import json
import sys
import urllib.error
import urllib.request

BASE = "https://charlieprotocol.fun"
MEASURED = "8FhAXv2tfXUpyMbJsHDHX9zfiEb9PERzFWSY9sgLpump"

# A real mainnet CA with a sharing config that this site has NOT pre-generated
# a page for. It is the common case: almost every CA anyone pastes is one of
# these, so it is the path that has to work, and it is the exact path that was
# returning "The page could not be found" while the post telling people to
# paste a CA was being retweeted.
UNMEASURED = "8KC4HMFfE6BPAPV1zzLpag6Brc5vBuqojfCj7wWApump"


def fetch(url: str) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "charlie-smoke/1"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


GATEWAY = "https://crowd-api-gateway.vercel.app/"


def gateway_check() -> tuple[int, str]:
    """crowd-api answers a real JSON-RPC call, and its admin reload refuses an
    unauthenticated caller. Both on the public URL the site depends on.
    """
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
        "params": [MEASURED, {"encoding": "base64"}],
    }).encode()
    req = urllib.request.Request(GATEWAY, data=payload,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = json.loads(r.read().decode())
    except Exception as exc:
        return 0, f"gateway unreachable: {exc}"
    if "result" not in body:
        return 0, f"gateway returned no result: {body}"

    # A valid empty JSON body, so the request reaches the auth check instead
    # of dying in Fastify's body parser -- an unparseable body returns 400
    # before the token is ever looked at, which would make this check pass
    # for the wrong reason if 400 were accepted.
    guard = urllib.request.Request(GATEWAY + "router/reload", data=b"{}",
                                   headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(guard, timeout=30):
            return 0, "router/reload accepted an unauthenticated request"
    except urllib.error.HTTPError as e:
        if e.code != 401:
            return 0, f"router/reload returned {e.code}, wanted 401"
    except Exception as exc:
        return 0, f"router/reload unreachable: {exc}"
    return 200, "ok"


# A coin whose split has never been changed, and the wallet that administers
# it. Read from mainnet; if that coin ever uses its one update, this check
# starts failing loudly, which is the correct outcome -- it means the fixture
# no longer tests what it claims to.
ENROLL_MINT = "JAMXU2JLraZ3RUhbgc3ttYPc18Kx4ojCnC56XR2zpump"
ENROLL_ADMIN = "Chx6EJ1QLRnhiyQHfpNNyiEWma8XPazbELPanPff4Nuj"
BURN_ADDRESS = "burn111111111111111111111111111111111111111"


def enroll_check(base: str) -> tuple[int, str]:
    """The enrollment API builds a transaction AND says it simulated cleanly.

    Checking only that the page loads would pass while every dev who tried to
    use it got an error, because all the work is behind this call.
    """
    ownership = f"{base}/api/enroll?mint={ENROLL_MINT}&authority={ENROLL_ADMIN}"
    status, body = fetch(ownership)
    try:
        seen = json.loads(body)
    except ValueError:
        return 0, f"ownership check returned non-JSON: {body[:120]}"
    if status != 200 or not seen.get("owns"):
        return 0, f"ownership check did not confirm the admin: {body[:160]}"

    # Every enrolled split carries the protocol's share, at the rate and to
    # the address the server names. Read from the response rather than
    # pinned here, so a deploy with the address unset fails THIS check with
    # the server's own words instead of a stale constant.
    toll = seen.get("toll") or {}
    if not toll.get("address"):
        return 0, "enrolment is not open: the server names no toll address"
    rest = 10_000 - int(toll["bps"]) - 2000
    build = (f"{base}/api/enroll?mint={ENROLL_MINT}&authority={ENROLL_ADMIN}"
             f"&shares={toll['address']}:{toll['bps']},{BURN_ADDRESS}:2000,{ENROLL_ADMIN}:{rest}")
    status, body = fetch(build)
    try:
        built = json.loads(body)
    except ValueError:
        return 0, f"build returned non-JSON: {body[:120]}"
    if status != 200 or not built.get("simulated") or not built.get("message"):
        return 0, f"build did not return a simulated transaction: {body[:200]}"

    # A wallet that is not the admin must be refused, or the page would let
    # anyone try to set anyone's split.
    status, body = fetch(f"{base}/api/enroll?mint={ENROLL_MINT}"
                         f"&authority={BURN_ADDRESS}&shares={BURN_ADDRESS}:10000")
    if status == 200:
        return 0, "a wallet that is not the admin was NOT refused"

    # And a split WITHOUT the protocol's share must be refused, or the toll
    # is a suggestion rather than the price of enrolling.
    status, body = fetch(f"{base}/api/enroll?mint={ENROLL_MINT}&authority={ENROLL_ADMIN}"
                         f"&shares={BURN_ADDRESS}:2000,{ENROLL_ADMIN}:8000")
    if status == 200:
        return 0, "a split without the protocol's share was NOT refused"
    return 200, "ok"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    base = ap.parse_args().base.rstrip("/")

    # (path, expected status, substrings that MUST appear, substrings that must NOT)
    checks = [
        ("/", 200, ["Charlie Protocol"], ["NOT_FOUND"]),
        ("/verify", 200, ["Verify a coin", 'name="mint"'], ["NOT_FOUND"]),
        # "1 coin observed" / "2 coins observed" -- assert the stem, not a
        # count or a plural, or this check breaks every time a coin is added.
        ("/coins", 200, ["observed", "Coins are measured when someone submits"],
         ["NOT_FOUND"]),
        (f"/verify?mint={MEASURED}", 200, [MEASURED, "Results"], ["NOT_FOUND"]),
        (f"/verify/{MEASURED}", 200, [MEASURED], ["NOT_FOUND"]),
        (f"/coin/{MEASURED}", 200, [MEASURED], ["NOT_FOUND"]),
        (f"/coin/{MEASURED}.json", 200, ['"mint"'], ["NOT_FOUND"]),
        # The case that shipped broken twice. A CA with no committed page must
        # come back as a real answer read from the chain at request time, not
        # a 404 and not a "we have not measured this" apology.
        (f"/verify?mint={UNMEASURED}", 200, [UNMEASURED, "Checks"],
         ["The page could not be found", "No page for that coin yet"]),
        (f"/verify/{UNMEASURED}", 200, [UNMEASURED], ["The page could not be found"]),
        (f"/coin/{UNMEASURED}", 200, [UNMEASURED], ["The page could not be found"]),
        # A coin that IS committed must keep its committed page: it carries
        # evidence-backed figures a live read cannot reach, so serving the
        # live version there would be a silent downgrade.
        (f"/verify?mint={MEASURED}", 200, ["17.584506254 SOL"], []),
        # Garbage must land on the paste box, never on a coin-shaped page.
        ("/verify?mint=notavalidmint", 400, ["Verify a coin"], []),
        # The raw-record link that every coin page carries, for a coin with no
        # committed record. It 404'd on the page most visitors see.
        (f"/coin/{UNMEASURED}.json", 200, ['"mint"', UNMEASURED],
         ["The page could not be found"]),
        # Enrollment: the page, and the API behind it answering for a real
        # coin. A dev arriving from a post lands here, and a broken build step
        # would mean a wallet is asked to sign nothing at all.
        ("/enroll", 200, ["Set your coin", "signAndSendTransaction"], ["NOT_FOUND"]),
        ("__enroll_api__", 200, [], []),
        ("/assets/charlie.png", 200, [], []),
        ("/assets/charlie-scanning.gif", 200, [], []),
        ("/assets/charlie-found.gif", 200, [], []),
        ("/assets/incinerator-stack.png", 200, [], []),
        ("/assets/incinerator-smoke.png", 200, [], []),
    ]

    # The RPC path itself. The site reads the chain through crowd-api; if that
    # gateway is down every /verify degrades to "could not read the chain",
    # and the site would still pass every route check above.
    checks.append(("__gateway__", 200, [], []))

    failures = []
    for path, want_status, must, must_not in checks:
        if path == "__gateway__":
            status, body = gateway_check()
        elif path == "__enroll_api__":
            status, body = enroll_check(base)
        else:
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

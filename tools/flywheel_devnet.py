"""Drive the deployed program on devnet and record what actually happened.

This is the proof that the flywheel turns. It does NOT model anything: every
number it writes down is read back off the chain after a landed transaction,
and the file it produces (`state/flywheel/devnet.json`) is the only input to
the page. A transaction that fails stops the run rather than producing a
figure.

    python -m tools.flywheel_devnet --keypair PATH [--rounds N]

What it does, in order:

1. `init_charlie_pool`, once, storing TOLL_BPS on chain.
2. `init_route(mint)` for a mock coin, with a dev split of 5/20/50.
3. Credit `collector(mint)` with lamports. **This is the one mocked step**,
   and it is mocked because pump is not on devnet: the fee ARRIVING is
   simulated by an ordinary transfer, and everything the protocol does with
   it afterwards is real. A page built from this file has to say so.
4. `distribute(mint)` -- permissionless, signed by a wallet that has no
   authority over any of the destinations.
5. Read every balance back and compare against arithmetic done here, not
   restated from the program.

The refusals are recorded too, because a page claiming "no caller can
redirect this" should show the refusals rather than assert them: a
substituted ops address, a substituted incinerator, and a dev split that
does not leave the toll alone. Those are simulated, not sent -- a
transaction that must fail should not be paid for.

Standard library only, like `indexer/`, and it signs with `indexer.ed25519`
and compiles with `indexer.message` -- the same code the rest of the project
uses, so a reader has one transaction builder to check and not two.
"""

from __future__ import annotations

import argparse
import base64
import json
import struct
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from indexer import ed25519, message
from indexer.base58 import decode as b58decode
from indexer.base58 import encode as b58encode
from indexer.base58 import pubkey_bytes
from indexer.curve import find_program_address

DEVNET = "https://api.devnet.solana.com"

PROGRAM_ID = "GFA3nG9geMhpPXaExVLGYBtj6aJX7S125dLzv4EcXGiG"
SYSTEM_PROGRAM = "11111111111111111111111111111111"
INCINERATOR = "1nc1nerator11111111111111111111111111111111"

# The constant the program compiles in, restated here so the check is a
# comparison. If the deployed program disagrees, the run says so.
TOLL_BPS = 2500

# The dev's split for the mock coin. Sums to 7500 == 10000 - TOLL_BPS, which
# is the invariant the program enforces on every write.
SOL_BURN_BPS = 500
OWN_BURN_BPS = 2000
OPS_BPS = 5000

OUT = Path(__file__).resolve().parents[1] / "state" / "flywheel" / "devnet.json"


class RpcError(RuntimeError):
    pass


# -- rpc ------------------------------------------------------------------


def rpc(method: str, params=None, *, tries: int = 6):
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}
    ).encode()
    last = None
    for attempt in range(tries):
        request = urllib.request.Request(
            DEVNET, data=body, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as handle:
                payload = json.loads(handle.read())
        except urllib.error.HTTPError as exc:
            last = f"HTTP {exc.code}"
        except OSError as exc:
            last = str(exc)
        else:
            if "error" in payload:
                raise RpcError(f"{method}: {payload['error']}")
            return payload.get("result")
        time.sleep(2 * (attempt + 1))
    raise RpcError(f"{method}: giving up after {tries} tries ({last})")


def balance(address: str) -> int:
    return rpc("getBalance", [address])["value"]


def account(address: str):
    return rpc("getAccountInfo", [address, {"encoding": "base64"}])["value"]


def account_after_write(address: str, *, tries: int = 20):
    """An account just created, read back.

    `getSignatureStatuses` reporting `confirmed` does not mean the next
    `getAccountInfo` sees the write: the read can be served by a node that
    is behind the one that confirmed. So this waits for the account to
    appear rather than assuming it is there, which is a real race and not a
    theoretical one -- the first run of this driver hit it.
    """
    for attempt in range(tries):
        existing = account(address)
        if existing is not None:
            return existing
        time.sleep(1.5 * (attempt + 1))
    raise RpcError(f"{address} did not appear after a confirmed write")


def balance_at_least(address: str, want: int, *, tries: int = 25) -> int:
    """A balance read that waits for a credit to be visible."""
    for attempt in range(tries):
        have = balance(address)
        if have >= want:
            return have
        time.sleep(2 * (attempt + 1))
    raise RpcError(f"{address} still below {want} lamports")


def balance_at_most(address: str, want: int, *, tries: int = 25) -> int:
    """A balance read that waits for a debit to be visible."""
    for attempt in range(tries):
        have = balance(address)
        if have <= want:
            return have
        time.sleep(2 * (attempt + 1))
    raise RpcError(f"{address} still above {want} lamports")


def transaction_logs(signature: str, *, tries: int = 25) -> list:
    """The log messages of a landed transaction.

    `getTransaction` answers null for a transaction that is confirmed but
    not yet finalized, and the confirmation this driver waits for is
    `confirmed`. So this waits for the record to appear rather than reading
    `None` and crashing, which is what the first run did.
    """
    for attempt in range(tries):
        landed = rpc(
            "getTransaction",
            [signature, {"encoding": "json", "maxSupportedTransactionVersion": 0}],
        )
        if landed:
            return (landed.get("meta") or {}).get("logMessages") or []
        time.sleep(2 * (attempt + 1))
    raise RpcError(f"{signature} has no transaction record to read logs from")


def blockhash() -> str:
    return rpc("getLatestBlockhash")["value"]["blockhash"]


def minimum_balance(space: int) -> int:
    return rpc("getMinimumBalanceForRentExemption", [space])


# -- keys -----------------------------------------------------------------


def load_keypair(path: Path) -> tuple[str, bytes]:
    """A Solana CLI keypair file: 64 bytes, seed then public key."""
    raw = bytes(json.loads(Path(path).read_text()))
    if len(raw) != 64:
        raise ValueError(f"{path}: expected 64 bytes, got {len(raw)}")
    seed = raw[:32]
    public = ed25519.public_key(seed)
    if public != raw[32:]:
        raise ValueError(f"{path}: public key does not match its seed")
    return b58encode(public), seed


# -- addresses ------------------------------------------------------------


def pda(seeds) -> str:
    return find_program_address(seeds, PROGRAM_ID)[0]


def addresses(mint: str) -> dict:
    return {
        "route": pda([b"route", pubkey_bytes(mint)]),
        "collector": pda([b"collect", pubkey_bytes(mint)]),
        "burn_pool": pda([b"burn", pubkey_bytes(mint)]),
        "charlie_pool": pda([b"charlie"]),
    }


# -- instruction data -----------------------------------------------------


def data_init_charlie_pool() -> bytes:
    return bytes([0])


def data_route(tag: int, sol_burn: int, own_burn: int, ops: int, ops_address: str) -> bytes:
    return bytes([tag]) + struct.pack("<HHH", sol_burn, own_burn, ops) + pubkey_bytes(ops_address)


def data_distribute() -> bytes:
    return bytes([3])


# -- instructions ---------------------------------------------------------


def ix_init_charlie_pool(payer: str, charlie_pool: str) -> message.Instruction:
    return (
        PROGRAM_ID,
        [
            (payer, True, True),
            (charlie_pool, False, True),
            (SYSTEM_PROGRAM, False, False),
        ],
        data_init_charlie_pool(),
    )


def ix_init_route(admin: str, mint: str, a: dict, ops_address: str) -> message.Instruction:
    return (
        PROGRAM_ID,
        [
            (admin, True, True),
            (mint, False, False),
            (a["route"], False, True),
            (a["collector"], False, True),
            (a["burn_pool"], False, True),
            (SYSTEM_PROGRAM, False, False),
        ],
        data_route(1, SOL_BURN_BPS, OWN_BURN_BPS, OPS_BPS, ops_address),
    )


def ix_distribute(mint: str, a: dict, ops_address: str, *, incinerator: str = INCINERATOR,
                  ops_override: str | None = None) -> message.Instruction:
    """`distribute`'s account list, in the order the program reads it.

    `incinerator` and `ops_override` exist so the refusal cases can pass
    something else and show the program refusing it.
    """
    return (
        PROGRAM_ID,
        [
            (mint, False, False),
            (a["route"], False, False),
            (a["collector"], False, True),
            (a["charlie_pool"], False, True),
            (a["burn_pool"], False, True),
            (incinerator, False, True),
            (ops_override or ops_address, False, True),
            (SYSTEM_PROGRAM, False, False),
        ],
        data_distribute(),
    )


def ix_transfer(sender: str, to: str, lamports: int) -> message.Instruction:
    return (
        SYSTEM_PROGRAM,
        [(sender, True, True), (to, False, True)],
        struct.pack("<IQ", 2, lamports),
    )


# -- sending --------------------------------------------------------------


def send(instructions, payer: str, seeds: list[bytes]) -> str:
    msg = message.compile_legacy(payer, list(instructions), blockhash())
    signatures = [ed25519.sign(seed, msg) for seed in seeds]
    wire = message.signed_transaction(msg, signatures)
    signature = rpc(
        "sendTransaction",
        [b58encode(wire), {"encoding": "base58", "preflightCommitment": "confirmed"}],
    )
    for _ in range(60):
        time.sleep(2)
        status = rpc("getSignatureStatuses", [[signature]])["value"][0]
        if status and status.get("confirmationStatus") in ("confirmed", "finalized"):
            if status.get("err"):
                raise RpcError(f"{signature} landed with error {status['err']}")
            return signature
    raise RpcError(f"{signature} did not confirm")


def simulate(instructions, payer: str) -> dict:
    msg = message.compile_legacy(payer, list(instructions), blockhash())
    wire = message.unsigned_transaction(msg)
    return rpc(
        "simulateTransaction",
        [
            b58encode(wire),
            {"encoding": "base58", "sigVerify": False, "replaceRecentBlockhash": True},
        ],
    )["value"]


def logs_of(result: dict) -> list:
    return result.get("logs") or []


# -- the arithmetic, done here rather than restated ----------------------


def expected_legs(l: int) -> dict:
    share = lambda bps: l * bps // 10_000
    legs = {
        "toll": share(TOLL_BPS),
        "sol_burn": share(SOL_BURN_BPS),
        "own_burn": share(OWN_BURN_BPS),
        "ops": share(OPS_BPS),
    }
    legs["remainder"] = l - sum(legs.values())
    return legs


def event_from_logs(logs) -> dict | None:
    """Parse the program's own distribute event out of its logs.

    The program emits it with `sol_log_data`, not a formatted string, and
    the runtime prints that as one `Program data:` line carrying each field
    as its OWN base64 value, space separated -- a tag then seven
    little-endian integers. (Not concatenated into a single blob, which is
    the obvious wrong guess; this was read off a simulation rather than
    assumed.) Formatting the figures as text cost enough of the deployed
    artifact to matter -- see the note at the emit site.

    Recording the program's own statement matters: the balances say what
    moved, and this says what the program believed it was doing. A page can
    show the two agreeing.
    """
    tag = b"charlie:distribute"
    names = ("l", "toll", "sol_burn", "own_burn", "ops", "remainder", "toll_bps")
    for line in logs:
        if not line.startswith("Program data: "):
            continue
        fields = line[len("Program data: "):].split()
        if not fields or base64.b64decode(fields[0]) != tag:
            continue
        if len(fields) < 1 + len(names):
            continue
        return {
            name: int.from_bytes(base64.b64decode(value), "little")
            for name, value in zip(names, fields[1:])
        }
    return None


# -- the run --------------------------------------------------------------


def ensure_charlie_pool(payer: str, seed: bytes, a: dict, record: dict) -> None:
    """`init_charlie_pool`, once. Idempotent: an existing pool is read and
    its stored toll checked, not re-initialised."""
    existing = account(a["charlie_pool"])
    if existing is None:
        signature = send([ix_init_charlie_pool(payer, a["charlie_pool"])], payer, [seed])
        record["init_charlie_pool"] = signature
        print(f"  init_charlie_pool   {signature}")
        existing = account_after_write(a["charlie_pool"])
    else:
        record["init_charlie_pool"] = "already initialised"
        print("  init_charlie_pool   already initialised")

    raw = base64.b64decode(existing["data"][0])
    stored = int.from_bytes(raw[0:2], "little")
    record["toll_bps_on_chain"] = stored
    if stored != TOLL_BPS:
        raise RpcError(f"charlie_pool stores toll {stored}, this driver expects {TOLL_BPS}")
    print(f"  toll_bps on chain   {stored}  (a quarter of the creator fee)")


def ensure_route(payer: str, seed: bytes, mint: str, a: dict, ops: str, record: dict) -> None:
    if account(a["route"]) is not None:
        record["init_route"] = "already initialised"
        print("  init_route          already initialised")
        return
    signature = send([ix_init_route(payer, mint, a, ops)], payer, [seed])
    record["init_route"] = signature
    print(f"  init_route          {signature}")


def read_route(a: dict) -> dict:
    raw = base64.b64decode(account_after_write(a["route"])["data"][0])
    return {
        "sol_burn_bps": int.from_bytes(raw[0:2], "little"),
        "own_burn_bps": int.from_bytes(raw[2:4], "little"),
        "ops_bps": int.from_bytes(raw[4:6], "little"),
        "ops_address": b58encode(raw[6:38]),
    }


def one_round(payer, seed, mint, a, ops, fee_lamports, index) -> dict:
    """Credit the collector, distribute, and read everything back.

    The credit is the mock: it stands in for pump's
    `distribute_creator_fees` paying an enrolled coin collector. Every
    number after it is the program own work.
    """
    print(f"\n  -- round {index}: {fee_lamports} lamports of creator fee --")

    before = {name: balance(address) for name, address in a.items()}
    before["ops"] = balance(ops)
    reserve = minimum_balance(0)

    credit = send([ix_transfer(payer, a["collector"], fee_lamports)], payer, [seed])
    print(f"  fee arrives         {credit}")

    # Wait for the credit to be visible before reading what is
    # distributable. A `confirmed` send does not guarantee the next
    # `getBalance` is served by a node that has it, and reading the
    # collector too early computes the split over the wrong `l` -- which is
    # exactly how this driver first disagreed with the chain by one round.
    collected = balance_at_least(a["collector"], before["collector"] + fee_lamports)
    distributable = collected - reserve

    result = simulate([ix_distribute(mint, a, ops)], payer)
    if result.get("err"):
        raise RpcError(f"distribute would fail: {result['err']} {logs_of(result)}")

    signature = send([ix_distribute(mint, a, ops)], payer, [seed])
    print(f"  distribute          {signature}")

    event = event_from_logs(transaction_logs(signature))
    if event is None:
        raise RpcError("the program logged no distribute event")

    # Same lagging-read problem on the way out: wait until the collector has
    # actually been debited before reading the destinations, so `moved` is
    # this round's work and not a mixture of two.
    balance_at_most(a["collector"], collected - event["toll"])
    after = {name: balance(address) for name, address in a.items()}
    after["ops"] = balance(ops)

    moved = {
        "toll": after["charlie_pool"] - before["charlie_pool"],
        "own_burn": after["burn_pool"] - before["burn_pool"],
    }
    expected = expected_legs(distributable)

    # The ops wallet is paid only once it can hold its share: an account
    # below its rent-exempt minimum cannot be left holding lamports, so the
    # program pays it zero and the share waits in the collector. The driver
    # applies the same rule rather than asserting the program's arithmetic
    # against a payment the runtime would have refused.
    ops_rent = minimum_balance(0)
    if expected["ops"] and before["ops"] + expected["ops"] < ops_rent:
        expected["remainder"] += expected["ops"]
        expected["ops"] = 0

    # The SOL burn leg cannot be read as a balance: the runtime destroys the
    # lamports at the end of the block, so the incinerator balance is always
    # zero. What proves it is the transfer inside the transaction, which is
    # in the event and in the inner instructions.
    #
    # The ops leg is not read as a balance either, and for a duller reason:
    # here the ops wallet IS the fee payer, so its delta also carries the
    # transaction fee. The event is the statement; the two legs that can be
    # read cleanly are read.
    for leg in ("toll", "own_burn"):
        if moved[leg] != expected[leg]:
            raise RpcError(f"{leg}: chain moved {moved[leg]}, arithmetic says {expected[leg]}")
    for leg in ("toll", "sol_burn", "own_burn", "ops", "remainder"):
        if event.get(leg) != expected[leg]:
            raise RpcError(
                f"{leg}: the program says {event.get(leg)}, arithmetic says {expected[leg]}"
            )

    print(f"  toll   -> charlie   {expected['toll']:>12,} lamports")
    print(f"  sol_burn -> incin.  {expected['sol_burn']:>12,} lamports  (destroyed)")
    print(f"  own_burn -> pool    {expected['own_burn']:>12,} lamports")
    print(f"  ops    -> wallet    {expected['ops']:>12,} lamports")
    print(f"  remainder stays     {expected['remainder']:>12,} lamports")

    return {
        "round": index,
        "fee_lamports": fee_lamports,
        "credit_signature": credit,
        "distribute_signature": signature,
        "distributable": distributable,
        "legs": expected,
        "event": event,
        "balances_moved": moved,
    }


def refusals(payer: str, mint: str, a: dict, ops: str) -> list:
    """The cases that must fail, shown failing. Simulated, not sent.

    A page that says no caller can redirect this is making a claim about an
    absence, and the only honest way to show an absence is to try the thing
    and print the refusal.
    """
    cases = []
    attacker = "Gk5Ti6WEwFiVnbrLPjvDjpbdqGWb2Jn9QvJnrtFSS8gL"

    for name, instruction, why in [
        (
            "a caller substitutes their own ops address",
            ix_distribute(mint, a, ops, ops_override=attacker),
            "ops_address is read out of route(mint), never taken from the caller",
        ),
        (
            "a caller substitutes the incinerator",
            ix_distribute(mint, a, ops, incinerator=attacker),
            "the SOL burn leg pays the incinerator, a constant in the program",
        ),
    ]:
        result = simulate([instruction], payer)
        error = result.get("err")
        cases.append({
            "case": name,
            "refused": error is not None,
            "error": json.dumps(error) if error else None,
            "log": next((line for line in logs_of(result) if "distribute:" in line), None),
            "why": why,
        })
        print(f"  {'REFUSED' if error else 'ACCEPTED -- BUG'}  {name}")

    # A dev split that does not leave the toll alone.
    #
    # This has to be `set_route` on the EXISTING route, not `init_route`.
    # Run against a route that already exists, `init_route` is refused with
    # `AccountAlreadyInitialized` -- refused, but for the wrong reason, which
    # would let a page claim the invariant is enforced on evidence that does
    # not show it. `set_route` reaches the bps check.
    name = "a dev sets their own shares to the whole fee"
    bad = (
        PROGRAM_ID,
        [
            (payer, True, True),
            (mint, False, False),
            (a["route"], False, True),
        ],
        data_route(2, 10_000, 0, 0, ops),
    )
    result = simulate([bad], payer)
    error = result.get("err")
    log = next((line for line in logs_of(result) if "route:" in line), None)
    cases.append({
        "case": name,
        "refused": error is not None,
        "error": json.dumps(error) if error else None,
        "log": log,
        "why": "the three dev shares must sum to 10000 - TOLL_BPS, enforced on every write",
    })
    print(f"  {'REFUSED' if error else 'ACCEPTED -- BUG'}  {name}")

    # And the same instruction with a LEGAL split must be accepted, or the
    # refusal above proves only that set_route rejects everything.
    name = "the dev moves their own three shares around"
    ok = (
        PROGRAM_ID,
        [
            (payer, True, True),
            (mint, False, False),
            (a["route"], False, True),
        ],
        data_route(2, 2_500, 2_500, 2_500, ops),
    )
    result = simulate([ok], payer)
    error = result.get("err")
    cases.append({
        "case": name,
        "refused": error is not None,
        "error": json.dumps(error) if error else None,
        "log": next((line for line in logs_of(result) if "route" in line), None),
        "why": "a split that leaves the toll alone is the dev's to choose, any day",
        "must_be_accepted": True,
    })
    print(f"  {'ACCEPTED' if not error else 'REFUSED -- BUG'}  {name}")
    return cases


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Prove the flywheel on devnet.")
    parser.add_argument("--keypair", required=True, type=Path)
    parser.add_argument(
        "--mint",
        default="So11111111111111111111111111111111111111112",
        help="the mock coin. Any pubkey: the program derives from it and never "
             "reads it, so devnet needs no real token.",
    )
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument(
        "--cranker", type=Path, default=None,
        help="a SECOND keypair, neither the coin's admin nor its ops wallet, "
             "used for one extra round to show distribute is permissionless. "
             "It needs a little SOL for fees and gets nothing back.",
    )
    parser.add_argument("--fee", type=int, default=400_000,
                        help="lamports of creator fee per round")
    args = parser.parse_args(argv)

    payer, seed = load_keypair(args.keypair)
    a = addresses(args.mint)
    ops = payer  # the dev ops wallet; the payer stands in for it here.

    print(f"program  {PROGRAM_ID}")
    print(f"payer    {payer}  {balance(payer) / 1e9:.6f} SOL")
    print(f"mint     {args.mint}")
    for name, address in a.items():
        print(f"  {name:14s} {address}")
    print()

    record = {
        "cluster": "devnet",
        "program_id": PROGRAM_ID,
        "mint": args.mint,
        "addresses": a,
        "toll_bps": TOLL_BPS,
        "dev_split": {
            "sol_burn_bps": SOL_BURN_BPS,
            "own_burn_bps": OWN_BURN_BPS,
            "ops_bps": OPS_BPS,
        },
        "ops_address": ops,
        "generated": int(time.time()),
    }

    ensure_charlie_pool(payer, seed, a, record)
    ensure_route(payer, seed, args.mint, a, ops, record)
    record["route_on_chain"] = read_route(a)

    rounds = []
    for index in range(1, args.rounds + 1):
        rounds.append(one_round(payer, seed, args.mint, a, ops, args.fee, index))
    record["rounds"] = rounds

    # One round cranked by a stranger. `distribute` taking no signer is the
    # claim, and a run whose only caller is also the ops wallet does not show
    # it -- that caller had a reason to call. This one gets nothing back.
    if args.cranker:
        cranker, cranker_seed = load_keypair(args.cranker)
        if cranker in (payer, ops):
            raise SystemExit("--cranker must not be the admin or the ops wallet")
        print(f"\n  -- extra round, cranked by a stranger: {cranker} --")
        before_charlie = balance(a["charlie_pool"])
        before_cranker = balance(cranker)
        credit = send([ix_transfer(payer, a["collector"], args.fee)], payer, [seed])
        balance_at_least(a["collector"], minimum_balance(0) + args.fee)
        signature = send([ix_distribute(args.mint, a, ops)], cranker, [cranker_seed])
        event = event_from_logs(transaction_logs(signature))
        if event is None:
            raise RpcError("the stranger's distribute logged no event")
        gained = balance_at_least(
            a["charlie_pool"], before_charlie + event["toll"]
        ) - before_charlie
        if gained != event["toll"]:
            raise RpcError("the stranger's distribute did not pay the toll")
        print(f"  fee arrives         {credit}")
        print(f"  distribute          {signature}")
        print(f"  toll   -> charlie   {event['toll']:>12,} lamports")
        print("  the caller is paid nothing: no leg is payable to them")
        record["permissionless"] = {
            "cranker": cranker,
            "is_admin": cranker == payer,
            "is_ops": cranker == ops,
            "credit_signature": credit,
            "distribute_signature": signature,
            "event": event,
            "charlie_pool_gained": gained,
            # The caller ends DOWN by the transaction fee. That is the point:
            # the crank's incentive is the ops share and the protocol's own
            # operation, never a bounty paid out of the legs (BUILD.md sec.5).
            "cranker_lamports_delta": balance(cranker) - before_cranker,
        }

    print("\n  -- what a caller cannot do --")
    record["refusals"] = refusals(payer, args.mint, a, ops)

    totals = {
        leg: sum(r["legs"][leg] for r in rounds)
        for leg in ("toll", "sol_burn", "own_burn", "ops", "remainder")
    }
    totals["fee"] = sum(r["fee_lamports"] for r in rounds)
    record["totals"] = totals
    record["charlie_pool_balance"] = balance(a["charlie_pool"])
    record["burn_pool_balance"] = balance(a["burn_pool"])

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

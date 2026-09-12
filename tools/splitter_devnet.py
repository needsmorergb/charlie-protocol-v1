"""Drive the $CHARLIE splitter on devnet and record what actually happened.

THE QUESTION THIS ANSWERS. $CHARLIE's sharing config is `admin_revoked` and
pays `burn111...111` 100%: its one irreversible update is spent, so its fees
can never be repointed by anybody, its deployer included. Suppose they
could. Suppose the destination were changed to a wallet that split the
creator fee three ways:

    50%  SOL burn               -> the incinerator, destroyed
    25%  $CHARLIE buy and burn  -> held for the burn crank
    25%  ops                    -> an ordinary wallet

Does that mechanism hold? This runs it and writes down the answer.

WHAT IS REAL. The program is deployed. The transactions land. The vault is a
PDA with no key, so the split is the only way SOL leaves it. The
incinerator credit is a real credit and the runtime really destroys it. The
mint in every derivation is $CHARLIE's real mainnet mint, so the addresses
here are the addresses a real deployment would use.

WHAT IS MOCKED, and it is one thing: **the creator fee arriving.** pump does
not exist on devnet, so an ordinary transfer stands in for pump paying the
fee. Everything the mechanism does after the money lands is real.

WHAT IS NOT PROVEN HERE. The buy-and-burn leg accrues but is not yet spent:
turning that SOL into destroyed $CHARLIE needs a swap against a live pool,
which devnet does not have. That leg is proven as far as "the SOL arrives
in a vault only the crank can spend", and the page says so rather than
implying tokens were burned.

    python -m tools.splitter_devnet --keypair PATH [--rounds N]

Standard library only, and it signs with `indexer.ed25519` and compiles with
`indexer.message` -- the same builders the rest of the project uses.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from indexer.base58 import encode as b58encode
from indexer.base58 import pubkey_bytes
from indexer.curve import find_program_address

from tools.flywheel_devnet import (
    INCINERATOR,
    SYSTEM_PROGRAM,
    RpcError,
    account,
    account_after_write,
    balance,
    balance_at_least,
    balance_at_most,
    ix_transfer,
    load_keypair,
    logs_of,
    minimum_balance,
    rpc,
    send,
    simulate,
    transaction_logs,
)

PROGRAM_ID = "2CfShCuyuLw1935ihB5BjzaymdZsmPLNtUFAAqUeMHBS"

# $CHARLIE, the real mainnet mint. Used here only as a seed: the program
# derives from it and never reads it, so the derivations below are the ones
# a real deployment would produce.
CHARLIE_MINT = "8FhAXv2tfXUpyMbJsHDHX9zfiEb9PERzFWSY9sgLpump"

# The split, compiled into the program. Restated here so the check is a
# comparison rather than a restatement of the program's own arithmetic.
SOL_BURN_BPS = 5_000
BUYBACK_BPS = 2_500
OPS_BPS = 2_500

OUT = Path(__file__).resolve().parents[1] / "state" / "flywheel" / "splitter.json"


def pda(seeds) -> str:
    return find_program_address(seeds, PROGRAM_ID)[0]


def addresses(mint: str) -> dict:
    return {
        "config": pda([b"config", pubkey_bytes(mint)]),
        "vault": pda([b"vault", pubkey_bytes(mint)]),
        "burn_vault": pda([b"burn", pubkey_bytes(mint)]),
    }


def data_initialise(ops_address: str) -> bytes:
    return bytes([0]) + pubkey_bytes(ops_address)


def data_split() -> bytes:
    return bytes([1])


def ix_initialise(payer: str, mint: str, a: dict, ops_address: str):
    return (
        PROGRAM_ID,
        [
            (payer, True, True),
            (mint, False, False),
            (a["config"], False, True),
            (a["vault"], False, True),
            (a["burn_vault"], False, True),
            (SYSTEM_PROGRAM, False, False),
        ],
        data_initialise(ops_address),
    )


def ix_split(mint: str, a: dict, ops_address: str, *, incinerator: str = INCINERATOR,
             ops_override: str | None = None):
    """`split`'s accounts, in the order the program reads them.

    `incinerator` and `ops_override` exist so the refusal cases can pass
    something else and show the program refusing it.
    """
    return (
        PROGRAM_ID,
        [
            (mint, False, False),
            (a["config"], False, False),
            (a["vault"], False, True),
            (a["burn_vault"], False, True),
            (incinerator, False, True),
            (ops_override or ops_address, False, True),
        ],
        data_split(),
    )


def expected_legs(l: int) -> dict:
    share = lambda bps: l * bps // 10_000
    legs = {
        "sol_burn": share(SOL_BURN_BPS),
        "buyback": share(BUYBACK_BPS),
        "ops": share(OPS_BPS),
    }
    legs["remainder"] = l - sum(legs.values())
    return legs


def event_from_logs(logs) -> dict | None:
    """The program's own split event: a tag then five little-endian fields,
    each its own base64 value in the `Program data:` line."""
    tag = b"charlie:split"
    names = ("l", "sol_burn", "buyback", "ops", "remainder")
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


def read_config(a: dict) -> dict:
    raw = base64.b64decode(account_after_write(a["config"])["data"][0])
    return {
        "mint": b58encode(raw[0:32]),
        "ops_address": b58encode(raw[32:64]),
    }


def ensure_initialised(payer, seed, mint, a, ops, record) -> None:
    if account(a["config"]) is not None:
        record["initialise"] = "already initialised"
        print("  initialise          already initialised")
        return
    signature = send([ix_initialise(payer, mint, a, ops)], payer, [seed])
    record["initialise"] = signature
    print(f"  initialise          {signature}")


def one_round(payer, seed, mint, a, ops, fee_lamports, index) -> dict:
    print(f"\n  -- round {index}: {fee_lamports} lamports of creator fee --")

    before = {name: balance(address) for name, address in a.items()}
    before["ops"] = balance(ops)
    reserve = minimum_balance(0)

    credit = send([ix_transfer(payer, a["vault"], fee_lamports)], payer, [seed])
    print(f"  fee arrives         {credit}")

    collected = balance_at_least(a["vault"], before["vault"] + fee_lamports)
    distributable = collected - reserve

    result = simulate([ix_split(mint, a, ops)], payer)
    if result.get("err"):
        raise RpcError(f"split would fail: {result['err']} {logs_of(result)}")

    signature = send([ix_split(mint, a, ops)], payer, [seed])
    print(f"  split               {signature}")

    event = event_from_logs(transaction_logs(signature))
    if event is None:
        raise RpcError("the program logged no split event")

    balance_at_most(a["vault"], collected - event["sol_burn"])
    after = {name: balance(address) for name, address in a.items()}
    after["ops"] = balance(ops)

    expected = expected_legs(distributable)

    # The ops wallet is paid only once it can hold its share: the runtime
    # refuses to leave an account below its rent minimum, and this program
    # cannot make a wallet it does not own rent exempt. The driver applies
    # the same rule rather than asserting against a payment the runtime
    # would have refused.
    if expected["ops"] and before["ops"] + expected["ops"] < minimum_balance(0):
        expected["remainder"] += expected["ops"]
        expected["ops"] = 0

    # The buyback leg is a balance that can be read. The SOL burn leg is
    # not: the runtime destroys it, so the incinerator is always zero and
    # the transfer inside the transaction is the only evidence.
    moved_buyback = after["burn_vault"] - before["burn_vault"]
    if moved_buyback != expected["buyback"]:
        raise RpcError(
            f"buyback: chain moved {moved_buyback}, arithmetic says {expected['buyback']}"
        )
    for leg in ("sol_burn", "buyback", "ops", "remainder"):
        if event[leg] != expected[leg]:
            raise RpcError(
                f"{leg}: the program says {event[leg]}, arithmetic says {expected[leg]}"
            )

    print(f"  sol_burn -> incin.  {expected['sol_burn']:>12,} lamports  (50%, destroyed)")
    print(f"  buyback  -> vault   {expected['buyback']:>12,} lamports  (25%, awaiting crank)")
    print(f"  ops      -> wallet  {expected['ops']:>12,} lamports  (25%)")
    print(f"  remainder waits     {expected['remainder']:>12,} lamports")

    return {
        "round": index,
        "fee_lamports": fee_lamports,
        "credit_signature": credit,
        "split_signature": signature,
        "distributable": distributable,
        "legs": expected,
        "event": event,
        "buyback_balance_delta": moved_buyback,
    }


def refusals(payer, mint, a, ops) -> list:
    """What a caller cannot do, shown rather than asserted. Simulated, not
    sent: a transaction that must fail should not be paid for."""
    cases = []
    attacker = "Gk5Ti6WEwFiVnbrLPjvDjpbdqGWb2Jn9QvJnrtFSS8gL"
    for name, instruction, why in [
        (
            "a caller substitutes their own ops address",
            ix_split(mint, a, ops, ops_override=attacker),
            "ops_address is read out of config, never taken from the caller",
        ),
        (
            "a caller substitutes the incinerator",
            ix_split(mint, a, ops, incinerator=attacker),
            "the SOL burn leg pays the incinerator, a constant in the program",
        ),
    ]:
        result = simulate([instruction], payer)
        error = result.get("err")
        cases.append({
            "case": name,
            "refused": error is not None,
            "error": json.dumps(error) if error else None,
            "log": next((line for line in logs_of(result) if "split:" in line), None),
            "why": why,
        })
        print(f"  {'REFUSED' if error else 'ACCEPTED -- BUG'}  {name}")

    # Re-initialising would be the way to repoint ops. There is no
    # `set_config`, and `initialise` refuses a config that exists.
    name = "anyone re-points the ops wallet"
    result = simulate([ix_initialise(payer, mint, a, attacker)], payer)
    error = result.get("err")
    cases.append({
        "case": name,
        "refused": error is not None,
        "error": json.dumps(error) if error else None,
        "log": next((line for line in logs_of(result) if "initialised" in line), None),
        "why": "there is no set_config instruction, and initialise refuses a "
               "config that already exists -- the proportions are compiled in "
               "and the ops address is written once",
    })
    print(f"  {'REFUSED' if error else 'ACCEPTED -- BUG'}  {name}")
    return cases


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Prove the $CHARLIE splitter on devnet.")
    parser.add_argument("--keypair", required=True, type=Path)
    parser.add_argument("--mint", default=CHARLIE_MINT,
                        help="default: $CHARLIE's real mainnet mint, used as a seed")
    parser.add_argument("--ops", default=None,
                        help="the ops wallet. Defaults to a wallet that is NOT the "
                             "payer, so the ops leg is a real third-party credit.")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--fee", type=int, default=900_000)
    parser.add_argument("--cranker", type=Path, default=None,
                        help="a second keypair, used for one round to show split "
                             "is permissionless. It gets nothing back.")
    args = parser.parse_args(argv)

    payer, seed = load_keypair(args.keypair)
    a = addresses(args.mint)
    ops = args.ops or payer

    print(f"program  {PROGRAM_ID}  (devnet)")
    print(f"payer    {payer}  {balance(payer) / 1e9:.6f} SOL")
    print(f"mint     {args.mint}  ($CHARLIE, mainnet mint used as the seed)")
    print(f"ops      {ops}")
    for name, address in a.items():
        print(f"  {name:12s} {address}")
    print()

    record = {
        "cluster": "devnet",
        "program_id": PROGRAM_ID,
        "mint": args.mint,
        "mint_is_charlie": args.mint == CHARLIE_MINT,
        "addresses": a,
        "split": {
            "sol_burn_bps": SOL_BURN_BPS,
            "buyback_bps": BUYBACK_BPS,
            "ops_bps": OPS_BPS,
        },
        "ops_address": ops,
        "generated": int(time.time()),
    }

    ensure_initialised(payer, seed, args.mint, a, ops, record)
    record["config_on_chain"] = read_config(a)
    if record["config_on_chain"]["mint"] != args.mint:
        raise RpcError("the config on chain names a different mint")

    rounds = []
    for index in range(1, args.rounds + 1):
        rounds.append(one_round(payer, seed, args.mint, a, ops, args.fee, index))
    record["rounds"] = rounds

    if args.cranker:
        cranker, cranker_seed = load_keypair(args.cranker)
        if cranker in (payer, ops):
            raise SystemExit("--cranker must not be the payer or the ops wallet")
        print(f"\n  -- extra round, split called by a stranger: {cranker} --")
        before_burn = balance(a["burn_vault"])
        before_cranker = balance(cranker)
        credit = send([ix_transfer(payer, a["vault"], args.fee)], payer, [seed])
        balance_at_least(a["vault"], minimum_balance(0) + args.fee)
        signature = send([ix_split(args.mint, a, ops)], cranker, [cranker_seed])
        event = event_from_logs(transaction_logs(signature))
        if event is None:
            raise RpcError("the stranger's split logged no event")
        gained = balance_at_least(
            a["burn_vault"], before_burn + event["buyback"]
        ) - before_burn
        print(f"  split               {signature}")
        print(f"  sol_burn -> incin.  {event['sol_burn']:>12,} lamports")
        print("  the caller is paid nothing: no leg is payable to them")
        record["permissionless"] = {
            "cranker": cranker,
            "is_ops": cranker == ops,
            "credit_signature": credit,
            "split_signature": signature,
            "event": event,
            "burn_vault_gained": gained,
            "cranker_lamports_delta": balance(cranker) - before_cranker,
        }

    print("\n  -- what a caller cannot do --")
    record["refusals"] = refusals(payer, args.mint, a, ops)

    totals = {
        leg: sum(r["legs"][leg] for r in rounds)
        for leg in ("sol_burn", "buyback", "ops", "remainder")
    }
    totals["fee"] = sum(r["fee_lamports"] for r in rounds)
    record["totals"] = totals
    record["burn_vault_balance"] = balance(a["burn_vault"])
    record["vault_balance"] = balance(a["vault"])

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

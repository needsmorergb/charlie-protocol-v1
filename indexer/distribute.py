"""The crank that pays an enrolled coin's shareholders, the protocol's included.

pump does not push creator fees anywhere. They accrue in the coin's creator
vault until somebody calls `distribute_creator_fees`, which pays every
shareholder of the sharing config their share in one instruction. The call
is permissionless -- there is no signer in the instruction at all, only a
wallet paying the network fee -- so anybody may run it for any coin, and
somebody has to, or the protocol's share is owed and never received.

This is that somebody: an operator keeper, PROTOCOL.md sec.5 option 3, run
from a wallet whose only role is to pay the fee. Every byte comes from pump's
own on-chain IDL (the discriminator, the account list and its order, the PDA
seeds), and the trailing remaining accounts -- the shareholders, in the
config's own order, which no IDL describes -- were established by simulation
in the deploy repository's `trace` workflow: a short or reordered list is
refused with 6054 rather than paid wrongly.

Graduated coins are refused. After graduation the creator fee collects as
wrapped SOL in the coin's AMM pool vault, and paying it out needs
`pump_amm::transfer_creator_fees_to_pump` first -- a payout attempted
without it moves nothing, measured. That instruction is not built here yet,
and this module says so rather than sending a transaction that pays nobody.

Nothing here signs unless handed a keypair. `--dry-run` builds and simulates.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass

from . import pump
from .base58 import pubkey_bytes
from .curve import find_program_address
from .message import compile_legacy, unsigned_transaction

PUMP_PROGRAM = pump.PUMP_PROGRAM
SYSTEM_PROGRAM = "11111111111111111111111111111111"

# sha256("global:distribute_creator_fees")[:8], from pump's on-chain IDL
# (6EF8rrec...). The instruction takes no arguments.
DISTRIBUTE_CREATOR_FEES = bytes.fromhex("a572670079cef751")

# Below this the crank does not bother: the network fee is 5,000 lamports and
# the program keeps a rent reserve in the vault, so a vault holding dust
# pays out nothing worth the fee.
DEFAULT_MIN_LAMPORTS = 5_000_000

# A simulation needs a fee payer that exists on chain: the runtime answers
# AccountNotFound for one that holds no SOL, before running a single
# instruction. Without a key there is no wallet of ours to name, so the
# crank simulates as pump's own fee wallet -- the account every pump trade
# pays its protocol fee into, which therefore exists and is funded for as
# long as pump does. `run` still reads its balance before trusting it.
STAND_IN_PAYER = "CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4xC9iM"


class DistributeError(ValueError):
    """A coin this crank will not pay, and why."""


def _pda(seeds, program):
    return find_program_address(seeds, program)[0]


def creator_vault(creator: str) -> str:
    """PDA(["creator-vault", bonding_curve.creator]) under pump. For an
    enrolled coin the bonding curve's creator IS its sharing config, moved
    there by create_fee_sharing_config."""
    return _pda([b"creator-vault", pubkey_bytes(creator)], PUMP_PROGRAM)


def accounts_for(mint: str, creator: str, config_address: str, shareholders) -> list:
    """`(address, is_signer, is_writable)` in the IDL's order, then the
    shareholders as remaining accounts in the config's order. No signer."""
    return [
        (mint, False, False),
        (pump.bonding_curve(mint), False, False),
        (config_address, False, False),
        (creator_vault(creator), False, True),
        (SYSTEM_PROGRAM, False, False),
        (_pda([b"__event_authority"], PUMP_PROGRAM), False, False),
        (PUMP_PROGRAM, False, False),
    ] + [(address, False, True) for address in shareholders]


def instruction(mint: str, creator: str, config_address: str, shareholders):
    return (PUMP_PROGRAM, accounts_for(mint, creator, config_address, shareholders),
            DISTRIBUTE_CREATOR_FEES)


@dataclass(frozen=True)
class Plan:
    mint: str
    config: str
    creator: str
    shareholders: tuple
    vault: str
    vault_lamports: int
    message: bytes


def plan(rpc, mint: str, payer: str, *, blockhash: str | None = None) -> Plan:
    """Read the coin and build the transaction, refusing what cannot be paid."""
    curve = pump.read_bonding_curve(rpc, mint)
    try:
        config = pump.read_sharing_config(rpc, curve)
    except pump.DecodeError as exc:
        if pump.NO_FEE_SPLIT_MARKER in str(exc):
            raise DistributeError(f"{mint} has no fee-sharing config: nothing to distribute") from None
        raise
    if curve.graduated:
        raise DistributeError(
            f"{mint} has graduated to the pump AMM. Its creator fee collects as "
            "wrapped SOL in the pool vault, and paying it out needs "
            "pump_amm::transfer_creator_fees_to_pump first, which this crank does "
            "not build yet. A distribute_creator_fees alone would pay nobody."
        )
    holders = tuple(address for address, _bps in config.shareholders)
    vault = creator_vault(curve.creator)
    lamports = rpc.balance(vault)
    if blockhash is None:
        blockhash = rpc.call("getLatestBlockhash", [{"commitment": "finalized"}])["value"]["blockhash"]
    message = compile_legacy(
        payer, [instruction(mint, curve.creator, config.address, holders)], blockhash
    )
    return Plan(mint, config.address, curve.creator, holders, vault, lamports, message)


def simulate(rpc, message: bytes) -> dict:
    result = rpc.call("simulateTransaction", [
        base64.b64encode(unsigned_transaction(message)).decode(),
        {"encoding": "base64", "sigVerify": False, "replaceRecentBlockhash": True,
         "commitment": "processed"},
    ])
    return (result or {}).get("value") or {}


def explain(value: dict) -> str:
    logs = " ".join(value.get("logs") or [])
    if "6054" in logs or "RemainingAccounts" in logs:
        return "pump refused the shareholder list: it must be exactly the config's shareholders in order."
    if "6052" in logs:
        return "a shareholder is a program, which pump will not pay."
    if value.get("err") == "AccountNotFound":
        return "the fee payer does not exist on chain: it holds no SOL, so nothing it pays for can run."
    return f"pump refused the distribution: {value.get('err')}"


def run(rpc, mints, *, payer: str, keypair=None, min_lamports: int = DEFAULT_MIN_LAMPORTS,
        send=None, confirm=None) -> list[dict]:
    """Plan, simulate and -- with a keypair -- send, one coin at a time.

    Every outcome is a row: skipped (with why), simulated, sent (with the
    signature). A failure on one coin never stops the next. A payer that
    holds no SOL is refused before the first coin, because every simulation
    would answer AccountNotFound and say nothing about the payouts.
    """
    if rpc.balance(payer) == 0:
        raise DistributeError(
            f"the fee payer {payer} holds no SOL, so nothing it pays for can be "
            "simulated or sent. Fund it, or simulate as a funded wallet with --payer."
        )
    rows = []
    for mint in mints:
        row = {"mint": mint}
        try:
            built = plan(rpc, mint, payer)
        except (DistributeError, pump.DecodeError, ValueError) as exc:   # ValueError: not even a mint
            row.update(outcome="skipped", reason=str(exc))
            rows.append(row)
            continue
        row.update(vault=built.vault, vault_lamports=built.vault_lamports,
                   shareholders=len(built.shareholders))
        if built.vault_lamports < min_lamports:
            row.update(outcome="skipped",
                       reason=f"vault holds {built.vault_lamports} lamports, below {min_lamports}")
            rows.append(row)
            continue
        value = simulate(rpc, built.message)
        if value.get("err") is not None:
            row.update(outcome="refused", reason=explain(value))
            rows.append(row)
            continue
        row["units"] = value.get("unitsConsumed")
        if keypair is None:
            row["outcome"] = "simulated"
            rows.append(row)
            continue
        signature = (send or _send)(rpc, keypair, built.message)
        row.update(outcome="sent", signature=signature)
        if confirm is not None:
            confirm(rpc, signature)
        rows.append(row)
    return rows


def _send(rpc, keypair, message: bytes) -> str:
    from .buyback import send_signed
    return send_signed(rpc, keypair, message)


def enrolled_mints(records) -> list[str]:
    """The mints whose committed record's PROTOCOL_SHARE check passed --
    the coins that are in the protocol, read off the same records the
    index is built from."""
    out = []
    for record in records:
        checks = record.get("checks") or []
        if any(c.get("name") == "PROTOCOL_SHARE" and c.get("status") == "PASS" for c in checks):
            if record.get("mint"):
                out.append(record["mint"])
    return sorted(set(out))

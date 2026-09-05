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

After graduation the creator fee collects as wrapped SOL in an AMM-side
vault that `distribute_creator_fees` cannot see: six coins measured 0
lamports distributed alone and their full balances after
`pump_amm::transfer_creator_fees_to_pump`, one of them 101.4 SOL (the
deploy repository's `graduated` workflow). So for a graduated coin the crank
is two instructions in one transaction -- the AMM transfer, then the
distribution -- and the transfer's ten accounts and their order come from
the AMM's on-chain IDL as that workflow printed them. It too has no signer.
The one graduated case still refused is a pool whose `coin_creator` is not
the coin's sharing config (a pool older than the field, or a coin that
graduated before it enrolled and was never migrated): the AMM routes that
coin's fee to the address the pool names, and moving it needs
`pump_amm::migrate_pool_coin_creator`, which this does not build.

Nothing here signs unless handed a keypair. `--dry-run` builds and simulates.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass

from . import legs, pump
from .base58 import pubkey_bytes
from .buyback import (
    ASSOCIATED_TOKEN_PROGRAM,
    WSOL_MINT,
    canonical_pool,
    coin_creator_vault_authority,
    decode_pool,
    decode_token_amount,
)
from .buyback import event_authority as amm_event_authority
from .curve import find_program_address
from .enroll import associated_token_address
from .message import compile_legacy, unsigned_transaction

PUMP_PROGRAM = pump.PUMP_PROGRAM
PUMP_AMM_PROGRAM = pump.PUMP_AMM_PROGRAM
TOKEN_PROGRAM = pump.TOKEN_PROGRAM
SYSTEM_PROGRAM = "11111111111111111111111111111111"

# sha256("global:distribute_creator_fees")[:8], from pump's on-chain IDL
# (6EF8rrec...). The instruction takes no arguments.
DISTRIBUTE_CREATOR_FEES = bytes.fromhex("a572670079cef751")
# sha256("global:transfer_creator_fees_to_pump")[:8], from the AMM's on-chain
# IDL (pAMMBay6...). No arguments, no signer: it unwraps the wSOL in the
# coin creator's AMM vault into pump's creator-vault for the same creator,
# and is a no-op when that vault is empty (measured on ten coins).
TRANSFER_CREATOR_FEES_TO_PUMP = bytes.fromhex("8b348655e4e56cf1")

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


def amm_vault(coin_creator: str, quote_mint: str = WSOL_MINT) -> str:
    """The AMM-side vault: the wSOL token account owned by the
    `creator_vault` PDA of `Pool.coin_creator`, which for an enrolled coin
    is its sharing config."""
    return associated_token_address(coin_creator_vault_authority(coin_creator), quote_mint, TOKEN_PROGRAM)


def transfer_accounts_for(coin_creator: str, quote_mint: str = WSOL_MINT) -> list:
    """`transfer_creator_fees_to_pump`'s accounts, in the IDL's order, as the
    deployed AMM declares them (the `graduated` workflow prints the list it
    resolves from the on-chain IDL; this is that list). No signer."""
    return [
        (quote_mint, False, False),
        (TOKEN_PROGRAM, False, False),
        (SYSTEM_PROGRAM, False, False),
        (ASSOCIATED_TOKEN_PROGRAM, False, False),
        (coin_creator, False, False),
        (coin_creator_vault_authority(coin_creator), False, True),
        (amm_vault(coin_creator, quote_mint), False, True),
        (creator_vault(coin_creator), False, True),
        (amm_event_authority(), False, False),
        (PUMP_AMM_PROGRAM, False, False),
    ]


def transfer_instruction(coin_creator: str, quote_mint: str = WSOL_MINT):
    return (PUMP_AMM_PROGRAM, transfer_accounts_for(coin_creator, quote_mint),
            TRANSFER_CREATOR_FEES_TO_PUMP)


@dataclass(frozen=True)
class Plan:
    mint: str
    config: str
    creator: str
    shareholders: tuple
    vault: str
    vault_lamports: int
    message: bytes
    instructions: tuple = ()
    # Graduated coins only: the AMM pool, the wSOL vault beside it and what
    # that vault holds, which the first instruction moves into `vault`.
    pool: str | None = None
    amm_vault: str | None = None
    amm_lamports: int = 0
    # What the split pays the protocol's wallet. The crank is permissionless
    # and the fee payer is ours, so a coin that does not pay the protocol is
    # not ours to pay for.
    toll_bps: int = 0

    @property
    def graduated(self) -> bool:
        return self.pool is not None

    @property
    def payable_lamports(self) -> int:
        """Everything the transaction can reach: the pump vault plus, after
        graduation, the wSOL waiting on the AMM side."""
        return self.vault_lamports + self.amm_lamports


def plan(rpc, mint: str, payer: str, *, blockhash: str | None = None) -> Plan:
    """Read the coin and build the transaction, refusing what cannot be paid."""
    curve = pump.read_bonding_curve(rpc, mint)
    try:
        config = pump.read_sharing_config(rpc, curve)
    except pump.DecodeError as exc:
        if pump.NO_FEE_SPLIT_MARKER in str(exc):
            raise DistributeError(f"{mint} has no fee-sharing config: nothing to distribute") from None
        raise
    holders = tuple(address for address, _bps in config.shareholders)
    vault = creator_vault(curve.creator)
    lamports = rpc.balance(vault)
    instructions = [instruction(mint, curve.creator, config.address, holders)]
    pool_address = amm_vault_address = None
    amm_lamports = 0
    if curve.graduated:
        pool_address, amm_vault_address, amm_lamports = _amm_side(rpc, mint, config.address)
        instructions.insert(0, transfer_instruction(config.address))
    if blockhash is None:
        blockhash = rpc.call("getLatestBlockhash", [{"commitment": "finalized"}])["value"]["blockhash"]
    message = compile_legacy(payer, instructions, blockhash)
    return Plan(mint, config.address, curve.creator, holders, vault, lamports, message,
                tuple(instructions), pool_address, amm_vault_address, amm_lamports,
                toll_bps=config.share_of(legs.TOLL_DESTINATION) if legs.TOLL_DESTINATION else 0)


def _amm_side(rpc, mint: str, config_address: str) -> tuple[str, str, int]:
    """Where a graduated coin's fee sits, and the checks that make moving it
    safe: the canonical wSOL pool exists, and its `coin_creator` is the
    coin's sharing config -- Pool.coin_creator on 344 of 344 graduated
    enrolled coins sampled, and the one exception found (a zero pubkey, a
    pool older than the field) routes the fee elsewhere until migrated."""
    pool_address = canonical_pool(mint, WSOL_MINT)
    vault_address = amm_vault(config_address, WSOL_MINT)
    pool_account, vault_account = rpc.accounts([pool_address, vault_address])
    if pool_account is None:
        raise DistributeError(
            f"{mint} has graduated but has no canonical wSOL pool at {pool_address}. "
            "A pool with another quote mint is not paid by this crank."
        )
    pool = decode_pool(pool_address, pool_account)
    if pool.quote_mint != WSOL_MINT:
        raise DistributeError(f"{mint}'s pool is quoted in {pool.quote_mint}, not wSOL: not paid by this crank.")
    if pool.coin_creator != config_address:
        raise DistributeError(
            f"{mint}'s pool names {pool.coin_creator} as its coin creator, not the sharing "
            f"config {config_address}. The AMM routes the fee to that address until "
            "pump_amm::migrate_pool_coin_creator is called, which this crank does not build."
        )
    amm_lamports = decode_token_amount(vault_account, expect_mint=WSOL_MINT) or 0
    return pool_address, vault_address, amm_lamports


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
                   shareholders=len(built.shareholders), graduated=built.graduated,
                   amm_lamports=built.amm_lamports, instructions=len(built.instructions),
                   toll_bps=built.toll_bps)
        if built.toll_bps < legs.TOLL_BPS:
            row.update(outcome="skipped",
                       reason=(f"not enrolled: its split pays the protocol wallet {built.toll_bps} bps, "
                               f"below {legs.TOLL_BPS}. The crank pays for enrolled coins only"))
            rows.append(row)
            continue
        if built.payable_lamports < min_lamports:
            where = (f"vault holds {built.vault_lamports} lamports and the AMM vault "
                     f"{built.amm_lamports}, together below {min_lamports}"
                     if built.graduated else
                     f"vault holds {built.vault_lamports} lamports, below {min_lamports}")
            row.update(outcome="skipped", reason=where)
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

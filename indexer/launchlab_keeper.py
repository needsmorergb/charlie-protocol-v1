"""Keeper for the LaunchLab rail (`launchlab/`, LAUNCHLAB-RAIL.md section 8, step 7).

Reads every `ll_route` the program owns, works out which cranks have
something to do, builds them, simulates each one, and sends only on
`--send`. Standard library only, like the rest of the indexer.

The four cranks, and when each runs:

  crank_curve        coin still on the LaunchLab curve; its creator fee
                     vault holds at least the quote's `min_crank`
  crank_amm_creator  coin graduated; the CPMM pool records at least
                     `min_crank` of quote-side creator fees
  crank_lp           coin graduated; `ll_signer` holds the pool's Fee Key.
                     Sent as v0 with the rail's lookup table (33 accounts do
                     not fit legacy with a compute-budget instruction).
                     `lp_fee_amount` is u64::MAX: the lock program clamps it
                     to what is available (measured on devnet).
  crank_platform     the platform fee vault holds at least `min_crank`

Every account list here is the one the program checks and that ran on
devnet on 18 September 2026 (LAUNCHLAB-RAIL.md section 8, steps 2, 3, 6).

The cranks are admin-only, so the keeper key is the platform admin. That is
deliberate until the own-burn buy has an on-chain price bound.
"""

from __future__ import annotations

import argparse
import base64
import json
import struct
import sys
from dataclasses import dataclass

from . import message
from .base58 import decode, pubkey_bytes
from .curve import find_program_address
from .ed25519 import Keypair
from .rpc import RpcClient

U64_MAX = (1 << 64) - 1

CLUSTERS = {
    "devnet": {
        "program": "4WgYTmPM9VHyFACSMdw4Bh9jkK9BETjWV5tNrDzuJWgX",
        "launchlab": "DRay6fNdQ5J82H7xV6uq2aV3mNrUZ1J4PgSKsWgptcm6",
        "cpmm": "DRaycpLY18LhpbydsBWbVJtxpNv9oXPgjRSfpF2bWpYb",
        "lock": "DRay25Usp3YJAi7beckgpGUC7mGJ2cR1AVPxhYfwVCUX",
        "lock_auth": "7qWVV8UY2bRJfDLP4s37YzBPKUkVB46DStYJBpYbQzu3",
        "lookup_table": "6dGcRtawe3W1QMKuWBaP3BoWpNcbJNT5fHJLr8xrMovx",
        "rpc": "https://api.devnet.solana.com",
    },
    "mainnet": {
        "program": "GM6ET1LceNLkHUWefD79eeeJzFRk8yQwfnR7pdYP5d6e",
        "launchlab": "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj",
        "cpmm": "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C",
        "lock": "LockrWmn6K5twhz3y9w1dQERbmgSaRkfnTeTKbpofwE",
        "lock_auth": "3f7GcQFG397GAaEnv51zR6tsTVihYRydnydDD1cXekxH",
        "lookup_table": None,  # printed by tools/launchlab_mainnet_setup; pass --lookup-table
        "rpc": "https://api.mainnet-beta.solana.com",
    },
}

SYSTEM = "11111111111111111111111111111111"
TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ATA = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
MEMO = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"
COMPUTE_BUDGET = "ComputeBudget111111111111111111111111111111"
INCINERATOR = "1nc1nerator11111111111111111111111111111111"
WSOL = "So11111111111111111111111111111111111111112"

TAG_CRANK_CURVE, TAG_CRANK_AMM_CREATOR, TAG_CRANK_LP, TAG_CRANK_PLATFORM = 11, 12, 13, 14


def pda(seeds: list[bytes], program: str) -> str:
    return find_program_address(seeds, program)[0]


def ata(owner: str, mint: str, token_program: str) -> str:
    return pda([pubkey_bytes(owner), pubkey_bytes(token_program), pubkey_bytes(mint)], ATA)


def compute_limit(units: int) -> message.Instruction:
    return (COMPUTE_BUDGET, [], bytes([2]) + struct.pack("<I", units))


def _key(raw: bytes, offset: int) -> str:
    from .base58 import encode
    return encode(raw[offset:offset + 32])


def _u64(raw: bytes, offset: int) -> int:
    return struct.unpack_from("<Q", raw, offset)[0]


# -- on-chain state (layouts from launchlab/src/state.rs and devnet reads) --

@dataclass
class Platform:
    admin: str
    raydium_config: str
    charlie_pool: str
    paused: bool

    @classmethod
    def parse(cls, raw: bytes) -> "Platform":
        return cls(_key(raw, 1), _key(raw, 33), _key(raw, 97), bool(raw[129]))


@dataclass
class Route:
    address: str
    mint: str
    quote_mint: str
    ops_address: str
    launchlab_pool: str
    cpmm_pool: str
    coin_burned: int
    lamports_burned: int
    ops_paid: int

    @classmethod
    def parse(cls, address: str, raw: bytes) -> "Route":
        return cls(address, _key(raw, 1), _key(raw, 33), _key(raw, 103), _key(raw, 135),
                   _key(raw, 167), _u64(raw, 199), _u64(raw, 207), _u64(raw, 215))


@dataclass
class CpmmPool:
    amm_config: str
    pool_creator: str
    vault0: str
    vault1: str
    lp_mint: str
    mint0: str
    mint1: str
    prog0: str
    prog1: str
    observation: str
    creator_fees0: int
    creator_fees1: int

    @classmethod
    def parse(cls, raw: bytes) -> "CpmmPool":
        k = [_key(raw, 8 + 32 * i) for i in range(10)]
        tail = 328 + 5 + 56
        return cls(*k, _u64(raw, tail + 8), _u64(raw, tail + 16))


class Rail:
    """Addresses derived for one cluster, and the four crank builders."""

    def __init__(self, cluster: str):
        c = CLUSTERS[cluster]
        if not c["program"]:
            raise SystemExit(f"the rail is not deployed on {cluster} yet")
        self.c = c
        self.program = c["program"]
        self.platform = pda([b"ll_platform"], self.program)
        self.signer = pda([b"ll_signer"], self.program)

    def quote_cfg(self, quote_mint: str) -> str:
        return pda([b"quote_cfg", pubkey_bytes(quote_mint)], self.program)

    def collect(self, mint: str) -> str:
        return pda([b"ll_collect", pubkey_bytes(mint)], self.program)

    # -- LaunchLab PDAs
    def ll(self, seeds: list[bytes]) -> str:
        return pda(seeds, self.c["launchlab"])

    def global_config(self, quote_mint: str) -> str:
        return self.ll([b"global_config", pubkey_bytes(quote_mint), bytes([0]), struct.pack("<H", 0)])

    def platform_fee_vault(self, raydium_config: str, quote_mint: str) -> str:
        return self.ll([pubkey_bytes(raydium_config), pubkey_bytes(quote_mint)])

    # -- cranks -------------------------------------------------------------
    def crank_curve(self, keeper: str, p: Platform, r: Route) -> message.Instruction:
        col = self.collect(r.mint)
        pool = self.ll([b"pool", pubkey_bytes(r.mint), pubkey_bytes(r.quote_mint)])
        metas = [
            (keeper, True, True), (self.platform, False, False), (self.quote_cfg(r.quote_mint), False, False),
            (r.address, False, True), (col, False, True),
            (self.ll([b"creator_fee_vault_auth_seed"]), False, False),
            (self.ll([pubkey_bytes(col), pubkey_bytes(r.quote_mint)]), False, True),
            (ata(col, r.quote_mint, TOKEN), False, True), (r.quote_mint, False, False), (TOKEN, False, False),
            (SYSTEM, False, False), (ATA, False, False), (self.c["launchlab"], False, False),
            (self.ll([b"vault_auth_seed"]), False, False), (self.global_config(r.quote_mint), False, False),
            (p.raydium_config, False, False), (pool, False, True), (ata(col, r.mint, TOKEN_2022), False, True),
            (self.ll([b"pool_vault", pubkey_bytes(pool), pubkey_bytes(r.mint)]), False, True),
            (self.ll([b"pool_vault", pubkey_bytes(pool), pubkey_bytes(r.quote_mint)]), False, True),
            (r.mint, False, True), (TOKEN_2022, False, False), (self.ll([b"__event_authority"]), False, False),
            (self.platform_fee_vault(p.raydium_config, r.quote_mint), False, True),
            (INCINERATOR, False, True), (r.ops_address, False, True),
        ]
        return (self.program, metas, bytes([TAG_CRANK_CURVE]))

    def crank_amm_creator(self, keeper: str, r: Route, pool: CpmmPool) -> message.Instruction:
        col = self.collect(r.mint)
        cpmm = self.c["cpmm"]
        metas = [
            (keeper, True, True), (self.platform, False, False), (self.quote_cfg(r.quote_mint), False, False),
            (r.address, False, True), (col, False, True), (cpmm, False, False),
            (pda([b"vault_and_lp_mint_auth_seed"], cpmm), False, False), (r.cpmm_pool, False, True),
            (pool.vault0, False, True), (pool.vault1, False, True), (pool.mint0, False, True), (pool.mint1, False, True),
            (pool.prog0, False, False), (pool.prog1, False, False),
            (ata(col, pool.mint0, pool.prog0), False, True), (ata(col, pool.mint1, pool.prog1), False, True),
            (ATA, False, False), (SYSTEM, False, False), (pool.amm_config, False, False),
            (pool.observation, False, True), (INCINERATOR, False, True), (r.ops_address, False, True),
            (pda([b"creator_fee_share", pubkey_bytes(col), pubkey_bytes(pool.amm_config)], cpmm), False, False),
        ]
        return (self.program, metas, bytes([TAG_CRANK_AMM_CREATOR]))

    def crank_lp(self, keeper: str, p: Platform, r: Route, pool: CpmmPool, nft_account: str, nft_mint: str) -> message.Instruction:
        col = self.collect(r.mint)
        cpmm, lock, lock_auth = self.c["cpmm"], self.c["lock"], self.c["lock_auth"]
        metas = [
            (keeper, True, True), (self.platform, False, False), (self.quote_cfg(r.quote_mint), False, False),
            (r.address, False, True), (col, False, True), (self.signer, False, True),
            (lock, False, False), (lock_auth, False, False), (nft_account, False, True),
            (pda([b"locked_liquidity", pubkey_bytes(nft_mint)], lock), False, True),
            (cpmm, False, False), (pda([b"vault_and_lp_mint_auth_seed"], cpmm), False, False),
            (r.cpmm_pool, False, True), (pool.lp_mint, False, True),
            (ata(self.signer, pool.mint0, pool.prog0), False, True), (ata(self.signer, pool.mint1, pool.prog1), False, True),
            (pool.vault0, False, True), (pool.vault1, False, True), (pool.mint0, False, True), (pool.mint1, False, True),
            (ata(lock_auth, pool.lp_mint, TOKEN), False, True), (TOKEN, False, False), (TOKEN_2022, False, False),
            (MEMO, False, False), (ATA, False, False), (SYSTEM, False, False),
            (ata(col, pool.mint0, pool.prog0), False, True), (ata(col, pool.mint1, pool.prog1), False, True),
            (pool.amm_config, False, False), (pool.observation, False, True), (INCINERATOR, False, True),
            (r.ops_address, False, True), (p.charlie_pool, False, True),
        ]
        return (self.program, metas, bytes([TAG_CRANK_LP]) + struct.pack("<Q", U64_MAX))

    def crank_platform(self, keeper: str, p: Platform, quote_mint: str = WSOL) -> message.Instruction:
        metas = [
            (keeper, True, True), (self.platform, False, False), (self.quote_cfg(quote_mint), False, False),
            (self.signer, False, True), (self.c["launchlab"], False, False),
            (self.ll([b"platform_fee_vault_auth_seed"]), False, False), (p.raydium_config, False, False),
            (self.platform_fee_vault(p.raydium_config, quote_mint), False, True),
            (ata(self.signer, quote_mint, TOKEN), False, True), (quote_mint, False, False), (TOKEN, False, False),
            (SYSTEM, False, False), (ATA, False, False), (p.charlie_pool, False, True),
        ]
        return (self.program, metas, bytes([TAG_CRANK_PLATFORM]))


# -- planning and sending ----------------------------------------------------

@dataclass
class Job:
    name: str
    route: str | None
    instructions: list[message.Instruction]
    v0: bool = False


def _account(rpc: RpcClient, address: str) -> bytes | None:
    info = rpc.accounts([address])[0]
    return base64.b64decode(info["data"][0]) if info else None


def _token_amount(raw: bytes | None) -> int:
    return _u64(raw, 64) if raw and len(raw) >= 72 else 0


def plan(rpc: RpcClient, rail: Rail, keeper: str) -> list[Job]:
    platform_raw = _account(rpc, rail.platform)
    if platform_raw is None:
        raise SystemExit("ll_platform not found: wrong cluster?")
    p = Platform.parse(platform_raw)
    if p.paused:
        return []
    jobs: list[Job] = []
    min_crank = _u64(_account(rpc, rail.quote_cfg(WSOL)) or bytes(113), 105)

    for row in rpc.call("getProgramAccounts", [rail.program, {"encoding": "base64", "filters": [{"dataSize": 225}]}]):
        r = Route.parse(row["pubkey"], base64.b64decode(row["account"]["data"][0]))
        if r.quote_mint != WSOL:
            continue  # stock quotes wait for build step 5
        col = rail.collect(r.mint)
        if r.cpmm_pool == "11111111111111111111111111111111":
            pool = _account(rpc, rail.ll([b"pool", pubkey_bytes(r.mint), pubkey_bytes(r.quote_mint)]))
            status = pool[17] if pool else None
            vault = _account(rpc, rail.ll([pubkey_bytes(col), pubkey_bytes(r.quote_mint)]))
            if status == 0 and _token_amount(vault) >= min_crank:
                jobs.append(Job("crank_curve", r.address, [compute_limit(300_000), rail.crank_curve(keeper, p, r)]))
            continue
        pool = CpmmPool.parse(_account(rpc, r.cpmm_pool))
        quote_fees = pool.creator_fees0 if pool.mint0 == r.quote_mint else pool.creator_fees1
        # Below min_crank the program claims but leaves the quote unrouted
        # (seen on devnet: 906 lamports, claim only); not worth a fee.
        if quote_fees >= min_crank:
            jobs.append(Job("crank_amm_creator", r.address,
                            [compute_limit(300_000), rail.crank_amm_creator(keeper, r, pool)]))
        for acct in rpc.call("getTokenAccountsByOwner", [rail.signer, {"programId": TOKEN}, {"encoding": "jsonParsed"}])["value"]:
            info = acct["account"]["data"]["parsed"]["info"]
            if info["tokenAmount"]["amount"] != "1":
                continue
            lock_raw = _account(rpc, pda([b"locked_liquidity", pubkey_bytes(info["mint"])], rail.c["lock"]))
            if lock_raw and _key(lock_raw, 64) == r.cpmm_pool:
                jobs.append(Job("crank_lp", r.address,
                                [compute_limit(400_000), rail.crank_lp(keeper, p, r, pool, acct["pubkey"], info["mint"])], v0=True))
    vault = _account(rpc, rail.platform_fee_vault(p.raydium_config, WSOL))
    if _token_amount(vault) >= min_crank:
        jobs.append(Job("crank_platform", None, [rail.crank_platform(keeper, p)]))
    return jobs


def _lookup_addresses(rpc: RpcClient, table: str) -> list[str]:
    from .base58 import encode
    raw = _account(rpc, table)
    return [encode(raw[i:i + 32]) for i in range(56, len(raw), 32)]


def build(rpc: RpcClient, rail: Rail, keeper: str, job: Job) -> bytes:
    blockhash = rpc.call("getLatestBlockhash", [{"commitment": "confirmed"}])["value"]["blockhash"]
    if job.v0:
        table = rail.c["lookup_table"]
        return message.compile_v0(keeper, job.instructions, blockhash, table, _lookup_addresses(rpc, table))
    return message.compile_legacy(keeper, job.instructions, blockhash)


def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m indexer.launchlab_keeper", description=__doc__.split("\n\n")[0])
    ap.add_argument("--cluster", choices=sorted(CLUSTERS), default="devnet")
    ap.add_argument("--keypair", required=True, help="JSON array (Solana CLI format) of the platform admin key")
    ap.add_argument("--rpc", help="override the RPC endpoint")
    ap.add_argument("--send", action="store_true", help="send; without it every crank is only simulated")
    ap.add_argument("--lookup-table", help="override the cluster's lookup table (mainnet: from the setup script)")
    args = ap.parse_args(argv)

    rail = Rail(args.cluster)
    if args.lookup_table:
        rail.c = dict(rail.c, lookup_table=args.lookup_table)
    if rail.c["lookup_table"] is None:
        raise SystemExit("no lookup table for this cluster: pass --lookup-table")
    rpc = RpcClient(endpoints=[args.rpc or rail.c["rpc"]])
    with open(args.keypair) as f:
        kp = Keypair.from_secret_bytes(bytes(json.load(f)))
    jobs = plan(rpc, rail, kp.address)
    if not jobs:
        print("nothing to crank")
    failed = 0
    for job in jobs:
        msg = build(rpc, rail, kp.address, job)
        wire = message.unsigned_transaction(msg)
        sim = rpc.call("simulateTransaction", [base64.b64encode(wire).decode(),
                                               {"encoding": "base64", "sigVerify": False, "replaceRecentBlockhash": True}])["value"]
        line = f"{job.name:18} {job.route or '-':44} cu={sim.get('unitsConsumed')}"
        if sim.get("err"):
            failed += 1
            print(line, "SIMULATION FAILED", json.dumps(sim["err"]))
            continue
        if not args.send:
            print(line, "ok (simulated)")
            continue
        signed = message.signed_transaction(msg, [kp.sign(msg)])
        sig = rpc.call("sendTransaction", [base64.b64encode(signed).decode(), {"encoding": "base64"}])
        print(line, "sent", sig)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run())

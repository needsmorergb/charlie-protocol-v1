"""Set up the LaunchLab rail on mainnet, signed by the upgrade-authority wallet.

    python -m tools.launchlab_mainnet_setup --keypair ~/.config/solana/id.json \
        --charlie-pool <ADDRESS> [--cpmm-config D4FPEr...] [--min-crank 10000000] [--send]

Run AFTER `solana program deploy` (LAUNCHLAB-MAINNET.md). Four steps, each
skipped when it is already done on chain, each simulated first, and sent only
with `--send`:

  1. init_platform           ll_platform: admin = this wallet, charlie_mint,
                             charlie_pool (fixed: changing it needs an upgrade)
  2. set_quote(WSOL)         enables SOL as the quote, with min_crank
  3. create_raydium_platform Charlie's LaunchLab platform: 0.25% platform fee,
                             0.50% creator fee, LP 100% to the platform NFT,
                             graduating into --cpmm-config
  4. lookup table            the accounts every crank shares, so crank_lp fits
                             in a transaction (prints its address for the keeper)

Standard library only, like `indexer/`.
"""

from __future__ import annotations

import argparse
import base64
import json
import struct
import sys
import time

from indexer import message
from indexer.base58 import encode, pubkey_bytes
from indexer.curve import find_program_address
from indexer.ed25519 import Keypair
from indexer.rpc import RpcClient

PROGRAM = "ENZrfqk89oSQ2fPHPmi6NSMuRDo7F3txEZhi8ZVbCHym"
LAUNCHLAB = "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj"
CPMM = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"
LOCK = "LockrWmn6K5twhz3y9w1dQERbmgSaRkfnTeTKbpofwE"
LOCK_AUTH = "3f7GcQFG397GAaEnv51zR6tsTVihYRydnydDD1cXekxH"
LOADER = "BPFLoaderUpgradeab1e11111111111111111111111"
ALT_PROGRAM = "AddressLookupTab1e1111111111111111111111111"
SYSTEM = "11111111111111111111111111111111"
TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ATA = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
MEMO = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"
INCINERATOR = "1nc1nerator11111111111111111111111111111111"
WSOL = "So11111111111111111111111111111111111111112"
CHARLIE_MINT = "8FhAXv2tfXUpyMbJsHDHX9zfiEb9PERzFWSY9sgLpump"
DEFAULT_CPMM_CONFIG = "D4FPEruKEHrG5TenZ2mpDGEfu1iUvTiqBxvpU8HLBvC2"  # index 0: 0.25% trade, 0.05% creator


def pda(seeds, program):
    return find_program_address(seeds, program)[0]


PLATFORM = pda([b"ll_platform"], PROGRAM)
SIGNER = pda([b"ll_signer"], PROGRAM)
PROGRAM_DATA = pda([pubkey_bytes(PROGRAM)], LOADER)
QUOTE_CFG = pda([b"quote_cfg", pubkey_bytes(WSOL)], PROGRAM)
RAY_PLATFORM = pda([b"platform_config", pubkey_bytes(SIGNER)], LAUNCHLAB)


def init_platform(wallet: str, charlie_pool: str, charlie_mint: str) -> message.Instruction:
    data = bytes([0]) + pubkey_bytes(wallet) + pubkey_bytes(charlie_mint) + pubkey_bytes(charlie_pool)
    return (PROGRAM, [(wallet, True, True), (PLATFORM, False, True), (PROGRAM_DATA, False, False), (SYSTEM, False, False)], data)


def set_quote(wallet: str, min_crank: int) -> message.Instruction:
    data = bytes([1, 1]) + bytes(32) + struct.pack("<HIQ", 0, 0, min_crank)
    return (PROGRAM, [(wallet, True, True), (PLATFORM, False, False), (QUOTE_CFG, False, True), (WSOL, False, False), (SYSTEM, False, False)], data)


def create_raydium_platform(wallet: str, cpmm_config: str, fund: int) -> message.Instruction:
    data = bytes([5]) + struct.pack("<Q", fund)
    return (PROGRAM, [(wallet, True, True), (PLATFORM, False, True), (SIGNER, False, True), (RAY_PLATFORM, False, True),
                      (cpmm_config, False, False), (SYSTEM, False, False), (LAUNCHLAB, False, False)], data)


def shared_accounts(charlie_pool: str, cpmm_config: str) -> list[str]:
    """Accounts every crank on this platform uses. Per-coin accounts stay
    static in the transaction; these alone take crank_lp well under the limit."""
    return [PLATFORM, QUOTE_CFG, SIGNER, LOCK, LOCK_AUTH, CPMM, pda([b"vault_and_lp_mint_auth_seed"], CPMM),
            TOKEN, TOKEN_2022, MEMO, ATA, SYSTEM, INCINERATOR, charlie_pool, WSOL, cpmm_config, RAY_PLATFORM,
            LAUNCHLAB, pda([b"vault_auth_seed"], LAUNCHLAB), pda([b"__event_authority"], LAUNCHLAB),
            pda([b"platform_fee_vault_auth_seed"], LAUNCHLAB), pda([b"creator_fee_vault_auth_seed"], LAUNCHLAB),
            pda([b"global_config", pubkey_bytes(WSOL), bytes([0]), struct.pack("<H", 0)], LAUNCHLAB)]


def lookup_table(wallet: str, slot: int) -> tuple[str, message.Instruction]:
    table, bump = find_program_address([pubkey_bytes(wallet), struct.pack("<Q", slot)], ALT_PROGRAM)
    data = struct.pack("<IQB", 0, slot, bump)
    return table, (ALT_PROGRAM, [(table, False, True), (wallet, True, False), (wallet, True, True), (SYSTEM, False, False)], data)


def extend_table(wallet: str, table: str, addresses: list[str]) -> message.Instruction:
    data = struct.pack("<IQ", 2, len(addresses)) + b"".join(pubkey_bytes(a) for a in addresses)
    return (ALT_PROGRAM, [(table, False, True), (wallet, True, False), (wallet, True, True), (SYSTEM, False, False)], data)


class Runner:
    def __init__(self, rpc: RpcClient, kp: Keypair, send: bool):
        self.rpc, self.kp, self.send = rpc, kp, send

    def exists(self, address: str) -> bytes | None:
        info = self.rpc.accounts([address])[0]
        return base64.b64decode(info["data"][0]) if info else None

    def run(self, label: str, instructions: list[message.Instruction]) -> bool:
        blockhash = self.rpc.call("getLatestBlockhash", [{"commitment": "confirmed"}])["value"]["blockhash"]
        msg = message.compile_legacy(self.kp.address, instructions, blockhash)
        sim = self.rpc.call("simulateTransaction", [base64.b64encode(message.unsigned_transaction(msg)).decode(),
                                                    {"encoding": "base64", "sigVerify": False, "replaceRecentBlockhash": True}])["value"]
        if sim.get("err"):
            print(f"{label}: SIMULATION FAILED {json.dumps(sim['err'])}")
            for line in (sim.get("logs") or [])[-8:]:
                print("   ", line)
            return False
        if not self.send:
            print(f"{label}: simulated ok ({sim.get('unitsConsumed')} CU); rerun with --send")
            return True
        signed = message.signed_transaction(msg, [self.kp.sign(msg)])
        sig = self.rpc.call("sendTransaction", [base64.b64encode(signed).decode(), {"encoding": "base64"}])
        for _ in range(60):
            status = self.rpc.call("getSignatureStatuses", [[sig]])["value"][0]
            if status and status.get("confirmationStatus") in ("confirmed", "finalized"):
                if status.get("err"):
                    print(f"{label}: FAILED {sig} {status['err']}")
                    return False
                print(f"{label}: confirmed {sig}")
                return True
            time.sleep(1)
        print(f"{label}: sent {sig}, not confirmed within 60s -- check it before rerunning")
        return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--keypair", required=True, help="the upgrade-authority wallet (Solana CLI JSON)")
    ap.add_argument("--charlie-pool", required=True, help="where the Charlie leg's SOL goes; fixed once set")
    ap.add_argument("--charlie-mint", default=CHARLIE_MINT)
    ap.add_argument("--cpmm-config", default=DEFAULT_CPMM_CONFIG)
    ap.add_argument("--min-crank", type=int, default=10_000_000, help="lamports; default 0.01 SOL")
    ap.add_argument("--fund", type=int, default=20_000_000, help="lamports for ll_signer's rent; default 0.02 SOL")
    ap.add_argument("--lookup-table", help="an existing table to reuse instead of creating one")
    ap.add_argument("--rpc", default="https://api.mainnet-beta.solana.com")
    ap.add_argument("--send", action="store_true")
    args = ap.parse_args(argv)

    with open(args.keypair) as f:
        kp = Keypair.from_secret_bytes(bytes(json.load(f)))
    r = Runner(RpcClient(endpoints=[args.rpc]), kp, args.send)
    print(f"wallet {kp.address}\nprogram {PROGRAM}\nll_platform {PLATFORM}\nll_signer {SIGNER}\nraydium platform {RAY_PLATFORM}\n")

    if not r.exists(PROGRAM_DATA):
        print("the program is not deployed yet: run the deploy step in LAUNCHLAB-MAINNET.md first")
        return 1
    platform = r.exists(PLATFORM)
    if platform is None:
        if not r.run("1 init_platform", [init_platform(kp.address, args.charlie_pool, args.charlie_mint)]):
            return 1
        if not args.send:
            print("steps 2-4 need step 1 on chain; rerun with --send to continue")
            return 0
        platform = r.exists(PLATFORM)
    else:
        print(f"1 init_platform: done (charlie_pool {encode(platform[97:129])})")
    if r.exists(QUOTE_CFG) is None:
        if not r.run("2 set_quote(WSOL)", [set_quote(kp.address, args.min_crank)]):
            return 1
    else:
        print("2 set_quote(WSOL): done")
    if r.exists(RAY_PLATFORM) is None:
        if not r.run("3 create_raydium_platform", [create_raydium_platform(kp.address, args.cpmm_config, args.fund)]):
            return 1
    else:
        print("3 create_raydium_platform: done")
    if args.lookup_table:
        print(f"4 lookup table: using {args.lookup_table}")
    else:
        slot = r.rpc.call("getSlot", [{"commitment": "finalized"}])
        table, create = lookup_table(kp.address, slot)
        accounts = shared_accounts(encode(platform[97:129]) if platform else args.charlie_pool, args.cpmm_config)
        if r.run("4 lookup table", [create, extend_table(kp.address, table, accounts)]) and args.send:
            print(f"\nlookup table {table}\nput it in indexer/launchlab_keeper.py CLUSTERS['mainnet']['lookup_table']")
    return 0


if __name__ == "__main__":
    sys.exit(main())

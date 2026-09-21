"""Create Charlie's Raydium LaunchLab platform, with no program of ours.

LAUNCHLAB-RAIL.md's decision of 20 September 2026: the rail ships as a
platform and pays creators nothing, so there is no creator share to route and
nothing for `charlie-launchlab` to do. This is the setup path that decision
needs and `launchlab_mainnet_setup.py` cannot provide -- that script deploys
the abandoned program and calls its `create_raydium_platform`, whose rate
comes from `CREATOR_FEE_RATE = 5_000`. Run today it would create the 1.00%
shape.

Raydium's `create_platform_config` takes an `admin` that signs and pays, and
which is ALSO the platform fee wallet, the NFT wallet, the transfer-fee
extension authority and the vesting wallet. All of those can be an ordinary
wallet. That is the whole reason no program is needed: the accounts the
program existed to be are accounts a key can hold.

WHAT THIS COSTS. One transaction and the rent for one platform config
account -- the "~0.03 for setup accounts and fees" line in
LAUNCHLAB-MAINNET.md's estimate, and none of the 1.542 + 1.542 beside it,
which was the program deploy.

WHAT THIS DOES NOT DO. It does not make the platform fee burn. Fees accrue
in `platform_fee_vault(platform_config, quote)` and stay there until
something claims them, swaps the quote to SOL and buys and burns $CHARLIE.
The claim-to-SOL step is `indexer/platform_fees.py`, a library that no CLI
or schedule calls yet; this script prints the vault address it reads.

    python -m tools.launchlab_platform --wallet <address>            # simulate
    python -m tools.launchlab_platform --keypair id.json --send      # send
"""
from __future__ import annotations

import argparse
import base64
import json
import struct
import sys
import time

from indexer import message
from indexer.base58 import pubkey_bytes
from indexer.curve import find_program_address
from indexer.ed25519 import Keypair
from indexer.rpc import RpcClient

LAUNCHLAB = "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj"
SYSTEM = "11111111111111111111111111111111"
WSOL = "So11111111111111111111111111111111111111112"

# Anchor sighash for `create_platform_config`, asserted against the same
# constant in launchlab/src/raydium.rs by this file's test.
DISC_CREATE_PLATFORM_CONFIG = bytes([176, 90, 196, 175, 253, 113, 220, 20])

# Raydium CPMM config the graduated pool uses. Index 8, decided 18 September
# 2026: 0.25% trade, 0.55% creator.
DEFAULT_CPMM_CONFIG = "EUZHCdd8H7nueb2wLpxUqyuemdbe8TUxfCWcxVfARvgw"

# The platform fee, in Raydium's units: 2,500 = 0.25% of the quote on every
# curve trade. This is the house line and the only fee this project takes.
PLATFORM_FEE_RATE = 2_500

# ZERO, and the point of the exercise. A non-zero creator rate is the 0.50%
# the four-leg design charged and routed to program-owned accounts. With no
# program there is nowhere for it to go that is not a wallet Charlie holds,
# and holding money owed to other people is what this decision declined to
# do. `main` refuses to build the instruction if this is ever not zero.
CREATOR_FEE_RATE = 0

# LP fees after graduation, as Fee Key NFT scales. LaunchLab refuses a
# creator slice (measured on devnet, LAUNCHLAB-RAIL.md sec.8), and with
# creators paid nothing there is no split to make: all of it to the platform
# NFT wallet, which is the admin below.
PLATFORM_SCALE, CREATOR_SCALE, BURN_SCALE = 1_000_000, 0, 0


def pda(seeds, program):
    return find_program_address(seeds, program)[0]


def platform_config(admin: str) -> str:
    return pda([b"platform_config", pubkey_bytes(admin)], LAUNCHLAB)


def platform_fee_vault(config: str, quote_mint: str) -> str:
    """Where the platform fee accrues, per quote. Seeds are the config and
    the quote mint with no prefix."""
    return pda([pubkey_bytes(config), pubkey_bytes(quote_mint)], LAUNCHLAB)


def _borsh_str(s: str) -> bytes:
    raw = s.encode("utf-8")
    return struct.pack("<I", len(raw)) + raw


def create_platform_config(admin: str, cpmm_config: str, name: str, web: str, img: str) -> message.Instruction:
    """Raydium's own instruction, built from a wallet rather than through a
    program. Every account the program would have been is `admin`."""
    if CREATOR_FEE_RATE != 0:
        raise SystemExit("CREATOR_FEE_RATE is not zero: this rail pays creators nothing (LAUNCHLAB-RAIL.md)")
    data = (
        DISC_CREATE_PLATFORM_CONFIG
        + struct.pack("<QQQQ", PLATFORM_SCALE, CREATOR_SCALE, BURN_SCALE, PLATFORM_FEE_RATE)
        + _borsh_str(name) + _borsh_str(web) + _borsh_str(img)
        + struct.pack("<QQ", CREATOR_FEE_RATE, 0)
    )
    config = platform_config(admin)
    return (LAUNCHLAB, [
        (admin, True, True),            # platform_admin: signs and pays
        (admin, False, False),          # platform_fee_wallet
        (admin, False, False),          # platform_nft_wallet
        (config, False, True),          # the config account this creates
        (cpmm_config, False, False),    # cpswap_config for graduated pools
        (SYSTEM, False, False),
        (admin, False, False),          # transfer-fee extension authority
        (admin, False, False),          # vesting wallet
    ], data)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--wallet", help="build and simulate as this address, signing nothing")
    p.add_argument("--keypair", help="keypair file; signs, and sends with --send")
    p.add_argument("--send", action="store_true", help="sign and send (needs --keypair)")
    p.add_argument("--cpmm-config", default=DEFAULT_CPMM_CONFIG)
    p.add_argument("--name", default="Charlie Protocol")
    p.add_argument("--web", default="https://charlieprotocol.fun")
    p.add_argument("--img", default="https://charlieprotocol.fun/assets/charlie.png")
    args = p.parse_args(argv)

    keypair = Keypair.from_file(args.keypair) if args.keypair else None
    admin = keypair.address if keypair else args.wallet
    if not admin:
        raise SystemExit("give --wallet ADDRESS (to simulate) or --keypair PATH (to sign)")
    if args.send and keypair is None:
        raise SystemExit("--send needs --keypair: an address alone cannot sign")

    config = platform_config(admin)
    rpc = RpcClient()
    if rpc.accounts([config])[0] is not None:
        print(f"platform config {config} already exists; nothing to do")
        return 0

    ix = create_platform_config(admin, args.cpmm_config, args.name, args.web, args.img)
    print(f"admin / fee wallet / NFT wallet : {admin}")
    print(f"platform config                 : {config}")
    print(f"platform fee vault (WSOL quote) : {platform_fee_vault(config, WSOL)}")
    print(f"platform fee {PLATFORM_FEE_RATE} bps-units, creator fee {CREATOR_FEE_RATE}, "
          f"LP scales {PLATFORM_SCALE}/{CREATOR_SCALE}/{BURN_SCALE}")

    blockhash = rpc.call("getLatestBlockhash", [{"commitment": "confirmed"}])["value"]["blockhash"]
    msg = message.compile_legacy(admin, [ix], blockhash)
    sim = rpc.call("simulateTransaction", [
        base64.b64encode(message.unsigned_transaction(msg)).decode(),
        {"encoding": "base64", "sigVerify": False, "replaceRecentBlockhash": True},
    ])["value"]
    if sim.get("err"):
        print(f"SIMULATION FAILED {json.dumps(sim['err'])}", file=sys.stderr)
        for line in (sim.get("logs") or [])[-10:]:
            print("   ", line, file=sys.stderr)
        return 1
    print(f"simulated OK, {sim.get('unitsConsumed')} compute units")

    if not args.send:
        print("not sent: pass --keypair and --send")
        return 0
    signed = message.signed_transaction(msg, [keypair.sign(msg)])
    signature = rpc.call("sendTransaction", [
        base64.b64encode(signed).decode(),
        {"encoding": "base64", "preflightCommitment": "confirmed"},
    ])
    print(f"sent {signature}")
    for _ in range(60):
        status = (rpc.call("getSignatureStatuses", [[signature]]) or {}).get("value", [None])[0]
        if status and status.get("confirmationStatus") in ("confirmed", "finalized"):
            if status.get("err"):
                print(f"FAILED {json.dumps(status['err'])}", file=sys.stderr)
                return 1
            print(f"confirmed. platform config {config} is live")
            return 0
        time.sleep(1)
    print("not confirmed within 60s; check the signature before rerunning", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

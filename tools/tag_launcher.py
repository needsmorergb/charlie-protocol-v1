"""Launch one coin the way an X tag will: the launch wallet pays and signs,
and the split sends the requester's share to the payout treasury.

    python -m tools.tag_launcher --name "Moon Dog" --ticker MDOG                   # simulate
    python -m tools.tag_launcher --name "Moon Dog" --ticker MDOG --image dog.png \\
        --handle alice --tweet-id 123 --keypair launch.json --send                 # launch
    python -m tools.tag_launcher --resume-split <mint> --keypair launch.json --send   # finish a split

Without --send nothing is signed or sent: the create transaction is simulated
against mainnet as the launch wallet (`legs.CHARLIE_LAUNCH_WALLET`). The split
cannot be simulated until the coin exists, so a simulation checks its size
and rows only. With --send the owner's launch-wallet key signs both, in order,
and the split is simulated before it is sent. The mint and the create
signature are printed the moment the create confirms, before the split, so
a failure after that point can always be finished with --resume-split.

The X side (reading tags, the eligibility checks, replying) is not here; this
is the step it calls once a tag has passed them (`indexer.tag_launch`).
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
from pathlib import Path

from indexer import buyback, launch, legs
from indexer import tag_bot
from indexer import tag_launch as tag
from indexer.ed25519 import Keypair
from indexer.rpc import DEFAULT_ENDPOINTS, RpcClient

PLACEHOLDER_URI = tag_bot.PLACEHOLDER_URI
SPLIT_COST_LAMPORTS = 10_000_000   # 0.01 SOL covers the split's rent and fee with room to spare


# The RPC steps live in `indexer.tag_bot` so the X bot runs the same ones.
_simulate = tag_bot.simulate_wire
_send = tag_bot.send_wire
_blockhash = tag_bot.blockhash


def _pin(request: tag.TagRequest, image: Path, handle: str, tweet_id: str) -> str:
    from api.launch import pin_metadata
    ctype = mimetypes.guess_type(image.name)[0] or "image/png"
    return tag_bot.pin_uri(pin_metadata, request, (image.name, ctype, image.read_bytes()), handle, tweet_id)


def request_of(name: str, ticker: str) -> tag.TagRequest | None:
    """The request, if `name` and `ticker` pass the very grammar a tag must
    (`tag_launch.parse`), else None."""
    parsed = tag.parse(f"@{tag.BOT_HANDLE} launch {name} ${ticker}")
    if parsed is None or parsed.name != " ".join(name.split()) or parsed.ticker != ticker.upper():
        return None
    return parsed


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tools.tag_launcher", description=__doc__.split("\n\n")[0])
    ap.add_argument("--name", help="the coin's name (a new launch)")
    ap.add_argument("--ticker", help="the coin's ticker (a new launch)")
    ap.add_argument("--resume-split", metavar="MINT", help="set the split on a coin whose create landed but split did not")
    ap.add_argument("--uri", help="a metadata URI already pinned (default: pin --image, or a placeholder to simulate)")
    ap.add_argument("--image", type=Path, help="the coin's image, pinned with pump's metadata service")
    ap.add_argument("--handle", default="", help="the requester's X handle, credited in the metadata")
    ap.add_argument("--tweet-id", default="", help="the tag's tweet ID, linked in the metadata")
    ap.add_argument("--keypair", help="the launch wallet's key file; needed with --send")
    ap.add_argument("--send", action="store_true")
    ap.add_argument("--rpc", help="comma-separated RPC URLs (default: env CHARLIE_RPC_URLS)")
    args = ap.parse_args(argv)

    if args.send and not args.keypair:
        ap.error("--send needs --keypair")
    keypair = Keypair.from_file(args.keypair) if args.keypair else None
    if keypair and keypair.address != legs.CHARLIE_LAUNCH_WALLET:
        ap.error(f"that key is {keypair.address}, not the launch wallet {legs.CHARLIE_LAUNCH_WALLET}")
    raw = args.rpc or os.environ.get("CHARLIE_RPC_URLS") or ""
    rpc = RpcClient(tuple(u.strip() for u in raw.split(",") if u.strip()) or DEFAULT_ENDPOINTS)

    if args.resume_split:
        # Finishing a coin that exists: the new-launch gates do not apply,
        # only enough SOL for the split itself.
        if not args.send:
            ap.error("--resume-split sends; it needs --keypair and --send")
        balance = rpc.balance(legs.CHARLIE_LAUNCH_WALLET)
        if balance < SPLIT_COST_LAMPORTS:
            print(f"refused (wallet_low): the launch wallet holds {balance} lamports; a split needs "
                  f"{SPLIT_COST_LAMPORTS}", file=sys.stderr)
            return 2
        request = request_of(args.name, args.ticker) if args.name and args.ticker else None
        return _split(rpc, keypair, request, args.resume_split, {"mint": args.resume_split})

    if not (args.name and args.ticker):
        ap.error("a new launch needs --name and --ticker")
    request = request_of(args.name, args.ticker)
    if request is None:
        ap.error("--name must be letters, digits and single spaces (1-32), --ticker 2-10 letters or digits")
    refused = tag.content_refusal(request) or tag.wallet_refusal(rpc.balance(legs.CHARLIE_LAUNCH_WALLET))
    if refused:
        print(f"refused ({refused.code}): {refused}", file=sys.stderr)
        return 2

    if args.uri:
        uri = args.uri
    elif args.send:
        if not (args.image and args.handle and args.tweet_id):
            ap.error("--send needs --uri, or --image with --handle and --tweet-id to pin one")
        uri = _pin(request, args.image, args.handle, args.tweet_id)
    else:
        uri = PLACEHOLDER_URI

    built = tag.build(request, uri, _blockhash(rpc))
    rows = [{"address": r.address, "bps": r.bps} for r in tag.split_rows()]
    out = {"mint": built.mint.address, "name": request.name, "ticker": request.ticker, "uri": uri,
           "launch_wallet": legs.CHARLIE_LAUNCH_WALLET, "split": rows,
           "create_bytes": launch.size_of(built.create), "split_bytes": launch.size_of(built.split)}

    create_wire = launch.partially_signed(built.create, built.mint)
    simulated = _simulate(rpc, create_wire)
    out["create_simulation"] = {"err": simulated.get("err"), "units": simulated.get("unitsConsumed")}
    if simulated.get("err") is not None:
        out["logs"] = (simulated.get("logs") or [])[-8:]
        print(json.dumps(out, indent=2))
        return 1
    if not args.send:
        out["sent"] = False
        print(json.dumps(out, indent=2))
        return 0

    signature = _send(rpc, tag.sign_create(built, keypair))
    print(json.dumps({"create_sent": signature, "mint": built.mint.address}), flush=True)
    buyback.confirm(rpc, signature)
    out["create_signature"] = signature
    print(json.dumps({"created": built.mint.address, "create_signature": signature,
                      "if_the_split_fails": f"--resume-split {built.mint.address}"}), flush=True)

    return _split(rpc, keypair, request, built.mint.address, out)


def _split(rpc, keypair: Keypair, request: tag.TagRequest | None, mint: str, out: dict) -> int:
    """The second transaction: fee config + split, simulated, then sent."""
    split = tag.split_message(mint, _blockhash(rpc))
    checked = _simulate(rpc, tag.sign_split(split, keypair))
    if checked.get("err") is not None:
        out["split_error"] = {"err": checked.get("err"), "logs": (checked.get("logs") or [])[-8:]}
        out["retry"] = f"the coin exists without its split; rerun with --resume-split {mint}"
        print(json.dumps(out, indent=2))
        return 1
    signature = _send(rpc, tag.sign_split(split, keypair))
    buyback.confirm(rpc, signature)
    out["split_signature"] = signature
    out["sent"] = True
    if request is not None:
        out["reply"] = tag.reply_text(request, mint)
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Where a coin's creator fees actually go, and which other coins share that
destination.

Written to settle one question about other fee-routing projects that this
repository could not answer from press coverage: a project claiming to send
"100% of creator fees" to a bot can do it two ways, and they are not equally
trustworthy.

    fee_share      bonding_curve.creator names a SharingConfig, and pump pays
                   the shareholders it lists. Enrolment is `update_fee_shares_v2`,
                   the one-shot instruction /enroll already calls.
    plain_creator  bonding_curve.creator is an ordinary address. Fees accrue to
                   whoever holds that key, and "routing" is whatever that holder
                   chooses to do after claiming. Nothing on chain constrains it.

Both look identical in a write-up. They differ on chain at exactly one byte:
the owner program of the account `bonding_curve.creator` names.

For every destination this also reports `keyless`, computed from the address
itself with `curve.is_on_curve`: a keyless address is program-derived and no
key can sign for it, an on-curve one belongs to somebody. A collector that is
on-curve is custody, whatever the marketing says.

Reads only. Nothing here signs, sends, or writes to the chain.

    python tools/trace_collector.py --rpc <url> <mint> [<mint> ...]
    python tools/trace_collector.py --rpc <url> --siblings <mint>
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from indexer import pump                                    # noqa: E402
from indexer.curve import is_on_curve                       # noqa: E402
from indexer.legs import recipient_kind                     # noqa: E402
from indexer.rpc import DEFAULT_ENDPOINTS, RpcClient       # noqa: E402

# The first shareholder's pubkey begins where the header ends. A memcmp here
# finds every config that pays this address FIRST -- which is the whole
# population when the split is one destination at 100%, and an undercount
# when it is not. Stated in the output rather than left for the reader to
# discover.
FIRST_SHAREHOLDER_OFFSET = pump.SHARING_CONFIG_HEADER_BYTES

ROUTE_FEE_SHARE = "fee_share"
ROUTE_PLAIN_CREATOR = "plain_creator"


@dataclass(frozen=True)
class Destination:
    address: str
    bps: int
    kind: str
    keyless: bool


@dataclass(frozen=True)
class Trace:
    mint: str
    creator: str
    route: str
    admin: str | None = None
    admin_revoked: bool | None = None
    destinations: tuple[Destination, ...] = ()
    creator_kind: str | None = None
    creator_keyless: bool | None = None

    @property
    def sole_destination(self) -> str | None:
        """The address taking the entire fee, or None when the split is shared.

        A single destination at 10000 bps is the shape worth following: it is
        what "100% of creator fees to the bot" looks like on chain, and its
        address is what other coins would share if the same operator runs them.
        """
        if self.route == ROUTE_PLAIN_CREATOR:
            return self.creator
        if len(self.destinations) == 1 and self.destinations[0].bps == 10_000:
            return self.destinations[0].address
        return None


def trace(rpc, mint: str) -> Trace:
    curve = pump.read_bonding_curve(rpc, mint)
    try:
        config = pump.read_sharing_config(rpc, curve)
    except pump.DecodeError:
        # Not an error: an ordinary creator address is one of the two answers
        # this tool exists to tell apart.
        account = rpc.accounts([curve.creator])[0]
        return Trace(
            mint=mint,
            creator=curve.creator,
            route=ROUTE_PLAIN_CREATOR,
            creator_kind=recipient_kind(account),
            creator_keyless=not is_on_curve(curve.creator),
        )

    addresses = [address for address, _bps in config.shareholders]
    accounts = rpc.accounts(addresses) if addresses else []
    destinations = tuple(
        Destination(
            address=address,
            bps=bps,
            kind=recipient_kind(account),
            keyless=not is_on_curve(address),
        )
        for (address, bps), account in zip(config.shareholders, accounts)
    )
    return Trace(
        mint=mint,
        creator=curve.creator,
        route=ROUTE_FEE_SHARE,
        admin=config.admin,
        admin_revoked=config.admin_revoked,
        destinations=destinations,
    )


def siblings(rpc, collector: str) -> dict:
    """Every sharing config paying `collector` first -- the other coins run by
    whoever operates that address.

    Returns `{"mints", "truncated"}`. `truncated` counts matches whose config
    has more than one shareholder, which the single-shareholder slice cannot
    decode: they DID match the filter, so they are reported as a count rather
    than silently dropped.
    """
    entries = rpc.program_accounts(
        pump.PUMP_FEE_SHARE_PROGRAM,
        filters=[
            {"memcmp": {"offset": 0, "bytes": pump.SHARING_CONFIG_DISCRIMINATOR_B58}},
            {"memcmp": {"offset": FIRST_SHAREHOLDER_OFFSET, "bytes": collector}},
        ],
        data_slice={"offset": 0, "length": pump.SINGLE_SHAREHOLDER_SLICE},
    )
    mints: list[str] = []
    truncated = 0
    for entry in entries:
        try:
            config = pump.decode_sharing_config(entry.get("pubkey"), entry.get("account"))
        except pump.TruncatedConfig:
            truncated += 1
            continue
        except pump.DecodeError:
            continue
        mints.append(config.mint)
    return {"mints": mints, "truncated": truncated}


def render(result: Trace) -> str:
    lines = [result.mint, f"  creator        {result.creator}"]
    if result.route == ROUTE_PLAIN_CREATOR:
        lines += [
            "  route          plain_creator (no fee-sharing config)",
            f"  creator is     {result.creator_kind}, "
            f"{'keyless' if result.creator_keyless else 'ON CURVE -- somebody holds this key'}",
            "  meaning        fees accrue to this address and nothing on chain",
            "                 constrains what happens after they are claimed",
        ]
        return "\n".join(lines)

    lines += [
        "  route          fee_share (pump pays the shareholders below)",
        f"  admin          {result.admin}",
        f"  admin_revoked  {result.admin_revoked}",
        "  destinations",
    ]
    for destination in result.destinations:
        lines.append(
            f"    {destination.bps / 100:>6.2f}%  {destination.address}  "
            f"{destination.kind}  {'keyless' if destination.keyless else 'ON CURVE'}"
        )
    sole = result.sole_destination
    if sole:
        lines.append(f"  shape          one destination at 100%: {sole}")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mints", nargs="*")
    parser.add_argument("--rpc", help="comma-separated RPC endpoints (env CHARLIE_RPC_URLS)")
    parser.add_argument(
        "--siblings",
        action="store_true",
        help="also enumerate other coins paying the same sole destination",
    )
    parser.add_argument("--limit", type=int, default=25, help="sibling mints to print")
    parser.add_argument(
        "--collector",
        help="skip the mints and enumerate every config paying THIS address first",
    )
    args = parser.parse_args(argv)

    raw = args.rpc or os.environ.get("CHARLIE_RPC_URLS") or ""
    endpoints = tuple(url.strip() for url in raw.split(",") if url.strip()) or DEFAULT_ENDPOINTS
    rpc = RpcClient(endpoints)

    if args.collector:
        found = siblings(rpc, args.collector)
        print(f"coins paying {args.collector} first")
        print(f"  matched        {len(found['mints']) + found['truncated']}")
        if found["truncated"]:
            print(f"  multi-holder   {found['truncated']} (matched, holders not decoded)")
        for mint in found["mints"][: args.limit]:
            print(f"    {mint}")
        return 0

    collectors: dict[str, str] = {}
    for mint in args.mints:
        try:
            result = trace(rpc, mint)
        except pump.DecodeError as exc:
            print(f"{mint}\n  unreadable     {exc}")
            continue
        print(render(result))
        sole = result.sole_destination
        if sole:
            collectors.setdefault(sole, mint)
        print()

    if not args.siblings:
        return 0

    for collector, seed in collectors.items():
        found = siblings(rpc, collector)
        print(f"coins paying {collector} first (seen from {seed})")
        print(f"  matched        {len(found['mints']) + found['truncated']}")
        if found["truncated"]:
            print(f"  multi-holder   {found['truncated']} (matched, holders not decoded)")
        for mint in found["mints"][: args.limit]:
            print(f"    {mint}")
        if len(found["mints"]) > args.limit:
            print(f"    ... {len(found['mints']) - args.limit} more")
        print("  limit          finds configs paying this address FIRST; a config")
        print("                 that pays it in a later position is not counted")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

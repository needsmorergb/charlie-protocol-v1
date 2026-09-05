"""Which coins are in the protocol, read off the chain rather than off disk.

The crank paid only coins with a committed record under `web/`, and a coin
got a record by being submitted to the coverage queue. A dev who enrolled
on the page had done the on-chain half and nothing here noticed: the hourly
run said "no coins to distribute for" while their split sat unpaid, and
their coin page stayed on "no burns recorded" because the walk had never
been pointed at the coin. Enrolled is a fact about a sharing config -- it
pays the toll wallet at least TOLL_BPS -- so this asks pump's fee-share
program for exactly those configs.

One `getProgramAccounts` per shareholder slot, a memcmp on the toll wallet's
bytes at that slot, and a data slice that ends at that slot so the answer
is a few hundred bytes per coin rather than a kilobyte. The enroll page
always puts the toll in the first row, so a coin enrolled there is found by
the first call; the next few catch a split somebody built by hand with the
toll further down. A config naming the toll beyond `DEFAULT_SLOTS` is not
found by this scan, and is still paid the moment its coin is named on the
command line or carries a committed record.
"""

from __future__ import annotations

import base64
import time
from pathlib import Path

from . import legs, pump
from .base58 import encode

DEFAULT_SLOTS = 8


def slot_offset(slot: int) -> int:
    """Where shareholder `slot`'s pubkey begins in a sharing config."""
    return pump.SHARING_CONFIG_HEADER_BYTES + slot * pump.SHAREHOLDER_RECORD_BYTES


def _data(account) -> bytes:
    raw = (account or {}).get("data")
    blob = raw[0] if isinstance(raw, (list, tuple)) else raw
    return base64.b64decode(blob) if blob else b""


def scan(rpc, *, toll: str | None = None, min_bps: int | None = None, slots: int = DEFAULT_SLOTS) -> dict[str, int]:
    """`mint -> bps` for every sharing config paying the toll wallet at
    least `min_bps`, summed over the first `slots` shareholder slots.

    A memcmp match is a byte comparison at an offset, not proof of a
    shareholder: bytes past the config's declared count are whatever the
    account held before, so a slot at or beyond the count is ignored.
    """
    toll = legs.TOLL_DESTINATION if toll is None else toll
    min_bps = legs.TOLL_BPS if min_bps is None else min_bps
    if not toll:
        return {}
    paid: dict[str, int] = {}
    for slot in range(slots):
        offset = slot_offset(slot)
        entries = rpc.program_accounts(
            pump.PUMP_FEE_SHARE_PROGRAM,
            filters=[
                {"memcmp": {"offset": 0, "bytes": pump.SHARING_CONFIG_DISCRIMINATOR_B58}},
                {"memcmp": {"offset": offset, "bytes": toll}},
            ],
            data_slice={"offset": 0, "length": offset + pump.SHAREHOLDER_RECORD_BYTES},
        )
        for entry in entries:
            data = _data((entry or {}).get("account"))
            if len(data) < offset + pump.SHAREHOLDER_RECORD_BYTES:
                continue
            count = int.from_bytes(data[76:80], "little")
            if slot >= count:
                continue
            mint = encode(data[11:43])
            bps = int.from_bytes(data[offset + 32 : offset + 34], "little")
            paid[mint] = paid.get(mint, 0) + bps
    return {mint: bps for mint, bps in sorted(paid.items()) if bps >= min_bps}


def mints(rpc, **options) -> list[str]:
    """The enrolled coins, sorted."""
    return sorted(scan(rpc, **options))


def index_new(rpc, registry, evidence, out_dir, *, site_url: str | None = None, limit: int = 5,
              scan_seconds: float | None = None, now=None, discover=None) -> list:
    """Every coin the chain says is enrolled and that has no committed record
    yet, measured and written exactly as a queue submission is.

    A dev who signs the enrollment on the page has done the on-chain half;
    before this, their coin page stayed live-rendered and never committed,
    so the walk never recorded its burns and the index never listed the
    coin. No issue is opened and no submission row is recorded: there was
    no request, and the page plus its record are the artifact. Lives here
    rather than in `intake` because the observation path must name no list
    of coins (COV-01); this adds coins to it and never keeps one out.
    `discover` is the scan (`mints` by default), injectable for tests.
    """
    from . import intake

    found = (discover or mints)(rpc)
    attempt_time = now() if callable(now) else (now if now is not None else time.time())
    seconds = intake.DEFAULT_SCAN_SECONDS if scan_seconds is None else scan_seconds
    outcomes = []
    for candidate in found:
        if len(outcomes) >= max(0, limit):
            break
        try:
            # The same boundary the queue crosses: a mint is a filename here.
            mint = intake.validate_mint(candidate)
        except intake.InvalidMint:
            continue
        if (Path(out_dir) / f"{mint}.json").exists():
            continue
        observed, reason, url_for_verdict = intake.measure(
            rpc, mint, registry, evidence, out_dir,
            attempt_time=attempt_time, scan_seconds=seconds, site_url=site_url,
        )
        outcomes.append(intake.Outcome(issue_number=None, issue_url=None, mint=mint, observed=observed,
                                       reason=reason, verdict_url=url_for_verdict))
    return outcomes

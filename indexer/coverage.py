"""Enumerating sharing configs from the fee-sharing program, and a

**D-36: `sweep()` and the `enumerate` subcommand are an OFFLINE RESEARCH
TOOL. They are on no automatic path and feed no published surface.** The
chain-wide crawl they were written for was cut (D-33) once it was measured:
603,345 coins have a sharing config and 97.0% of them have a single
shareholder taking 100%, so the sweep spent the RPC budget and the
repository on coins that never asked, to check a split most of them do not
have. Intake is submission-driven instead.

They are kept because they work, they are tested, and they are the only way
to answer "how many coins have a config at all" if that is ever asked again.
Wiring either back into a cadence or onto a page is a NEW decision, not a
restoration — in particular `site.coverage_statement()` may not state their
counts, because a denominator this project no longer measures has no
business beside numbers it does (D-35).

Originally: enumerating sharing configs from the fee-sharing program, and a
config-only observation for a coin that has never been deep-observed
(supports COV-01 without an extra RPC round trip).

RESEARCH.md Pitfall 1, verified live: `RpcClient`'s endpoint penalty is
per-endpoint, not per-method. `sweep()` must be handed a DEDICATED
`RpcClient` -- never the same instance a per-coin observation loop shares --
or the one-shot enumeration call's cooldown on a refusing endpoint starves
that loop for 30-120 seconds at exactly the moment its call volume is
highest.

This module never `print`s. `sweep()` takes an `on_progress` callable and
lets `cli.py` own the printing, so this stays out of
`publish.NON_FIGURE_EMITTERS`'s way -- it emits no figure and no text of its
own.
"""

from __future__ import annotations

import time

from . import invariants
from .base58 import encode
from .legs import split_of
from .observe import Observation
from .pump import (
    PUMP_FEE_SHARE_PROGRAM,
    SHARING_CONFIG_DISCRIMINATOR_B58,
    SINGLE_SHAREHOLDER_SLICE,
    DecodeError,
    TruncatedConfig,
    decode_sharing_config,
)

# The discriminator memcmp -- every enumeration filter set starts here.
# Verified live (03-RESEARCH.md Q1): matches 603,325+ accounts and is the
# only filter needed to identify a SharingConfig at all.
CONFIG_FILTER = {"memcmp": {"offset": 0, "bytes": SHARING_CONFIG_DISCRIMINATOR_B58}}

# Byte offsets verified live against mainnet, both usable as memcmp filters
# alongside CONFIG_FILTER: admin_revoked is a single byte at 75, the
# shareholder-count u32 is at 76.
ADMIN_REVOKED_OFFSET = 75
SHAREHOLDER_COUNT_OFFSET = 76


def _narrowing_filters(*, holders: int | None, revoked: bool | None) -> list[dict]:
    """`CONFIG_FILTER` plus the optional narrowing filters a caller asks
    for -- never required, since the discriminator alone already identifies
    every sharing config.
    """
    filters = [CONFIG_FILTER]
    if holders is not None:
        filters.append(
            {
                "memcmp": {
                    "offset": SHAREHOLDER_COUNT_OFFSET,
                    "bytes": encode(int(holders).to_bytes(4, "little")),
                }
            }
        )
    if revoked is not None:
        filters.append(
            {
                "memcmp": {
                    "offset": ADMIN_REVOKED_OFFSET,
                    "bytes": encode(bytes([1 if revoked else 0])),
                }
            }
        )
    return filters


def sweep(rpc, evidence, *, holders: int | None = None, revoked: bool | None = None, on_progress=None) -> dict:
    """One `getProgramAccounts` call against the fee-sharing program,
    decoded through the one guarded decoder (`pump.decode_sharing_config`)
    and recorded append-only (`evidence.record_sharing_config`).

    `rpc` MUST be a dedicated client -- see the module docstring's warning.
    Requests the 114-byte single-shareholder slice (`pump.SINGLE_SHAREHOLDER_SLICE`):
    the complete account for the 97.0% single-shareholder majority, and a
    deliberate truncation for the rest -- `TruncatedConfig` catches those and
    they are counted, not recorded wrong.

    Returns `{"returned", "decoded", "truncated", "refused",
    "truncated_addresses"}` -- decoded + truncated + refused always equals
    returned, because every entry `getProgramAccounts` handed back takes
    exactly one of those three paths.
    """
    filters = _narrowing_filters(holders=holders, revoked=revoked)
    entries = rpc.program_accounts(
        PUMP_FEE_SHARE_PROGRAM,
        filters=filters,
        data_slice={"offset": 0, "length": SINGLE_SHAREHOLDER_SLICE},
    )
    if on_progress:
        on_progress(f"enumerated {len(entries)} account(s)")

    decoded = 0
    refused = 0
    truncated_addresses: list[str] = []
    for entry in entries:
        address = entry.get("pubkey")
        account = entry.get("account")
        try:
            config = decode_sharing_config(address, account)
        except TruncatedConfig as exc:
            truncated_addresses.append(exc.address)
            continue
        except DecodeError:
            refused += 1
            continue
        evidence.record_sharing_config(config)
        decoded += 1

    return {
        "returned": len(entries),
        "decoded": decoded,
        "truncated": len(truncated_addresses),
        "refused": refused,
        "truncated_addresses": truncated_addresses,
    }


def config_observation(config, registry=None, now=None) -> Observation:
    """An `Observation` for a config obtained from enumeration alone -- no
    bonding-curve read, no extra RPC round trip. This is COV-01's
    on-demand path unchanged: `observe.observe(config=...)` performs the
    same computation for a coin whose curve has already been read; this
    function is what a coverage sweep uses when it has nothing BUT the
    config in hand.

    Honest limit, stated rather than assumed: the checks tuple here is
    exactly `CONFIG_MINT` and `SPLIT_SUM`. `CONFIG_MINT` compares the
    config's own recorded mint against the mint we asked about -- and here
    the mint we asked about came out of that same config, so for an
    enumerate-only row that check cannot fail. `SPLIT_SUM` can, and does the
    real work. A surface rendering this observation must state that the two
    checks are not equally strong, rather than let a reader assume they are.
    """
    observed_at = now() if callable(now) else (now if now is not None else time.time())
    split = split_of(config, registry)
    record = Observation(mint=config.mint, observed_at=observed_at)
    record.config = config
    record.split = split
    record.checks = (
        invariants.config_mint(config.mint, config),
        invariants.split_sum(split),
    )
    record.verdict = invariants.apply_silence_rule(record.checks)
    return record

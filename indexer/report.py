"""Rendering an observation for a human.

ARCHITECTURE.md sec.4: "every number displays the check that backs it, and a
failed check is rendered *louder* than a passing one." The webapp is not built,
but the rule is not a webapp rule -- it applies to the first surface that
displays a number, which is this one.

So: passing checks are indented and quiet, failures carry a `!!` gutter and
their detail, and the report ends with what may be published rather than with a
total. The last line of a report about fee routing should be about permission
to speak, not about a number.

PUB-01/PUB-02: every number that is a *figure* (a name in `invariants.FIGURES`)
reaches this text through `publish.Publisher`, never through a direct read of
an `Observation` field -- that direct read is the bypass PUB-01 forbids.
Observed facts that are not figures (a mint's decimals, a raw SOL burn balance
before reconciliation, a check's own `expected`/`actual`) are unaffected; the
rule is about the names in `invariants.FIGURES`, nothing else.
"""

from __future__ import annotations

from . import invariants, publish

_GUTTER = {invariants.PASS: "  ok  ", invariants.FAIL: "!!FAIL", invariants.UNCHECKED: "  --  "}


def _lamports(value) -> str:
    if value is None:
        return "unknown"
    return f"{value / 1_000_000_000:.9f} SOL ({value} lamports)"


def _format_figure(name: str, value) -> str:
    if value is None:
        return "unknown"
    if name == invariants.SPLIT:
        return f"SOL burn {value['sol_burn']:>5}    BURN {value['burn']:>5}    OPS {value['paid']:>5}"
    if name in (invariants.SOL_BURN_TOTAL, invariants.OPS_TOTAL, invariants.BURN_TOTAL):
        return _lamports(value)
    if name == invariants.SUPPLY_DESTROYED:
        return f"{value} raw units"
    return str(value)


def render(observation) -> str:
    out = []
    add = out.append
    publisher = publish.Publisher(observation)

    add(f"mint        {observation.mint}")
    if observation.error and observation.config is None:
        add("")
        add(f"!! no observation: {observation.error}")
        add("")
        add("Recorded as a failed observation. A tick that could not read the chain is")
        add("part of the record, not an absence from it.")
        return "\n".join(out)

    config = observation.config
    add(f"config      {config.address}  (v{config.version}, status {config.status})")
    add(f"admin       {config.admin}")
    if config.admin_revoked:
        add("            admin REVOKED -- this split is permanent. Only pump can reset it.")
    else:
        add("            admin live -- this split can be changed at any time.")
    add(f"graduated   {'yes' if observation.graduated else 'no'}")

    split = observation.split
    add("")
    add("THE FACT -- the split, in bps, read off the sharing config")
    try:
        value, backs = publisher.figure(invariants.SPLIT)
        add(f"    {_format_figure(invariants.SPLIT, value)}    (backed by {', '.join(backs)})")
        for attribution in split.attributions:
            add("")
            add(f"    {attribution.bps:>5} bps  {attribution.leg.upper():<5} {attribution.address}")
            add(f"              {'program-derived' if attribution.keyless else 'not program-derived'}: {attribution.reason}")
    except publish.Withheld as exc:
        name, status, detail = exc.reasons[0]
        add(f"    withheld -- {name} ({status})")
        add(f"    {detail}")
    if observation.sol_burn_balances:
        add("")
        for address, lamports in observation.sol_burn_balances.items():
            add(f"    balance   {address}")
            add(f"              {_lamports(lamports)}  (observed, NOT reconciled -- see SOL_BURN_BALANCE)")

    state = observation.mint_state
    add("")
    add("MINT STATE")
    add(f"    supply    {state.ui_supply:,.{state.decimals}f}  ({state.supply} base units, {state.decimals} decimals)")
    add(f"    authority {'revoked' if state.mint_authority is None else state.mint_authority}")
    add(f"    freeze    {'revoked' if state.freeze_authority is None else state.freeze_authority}")

    add("")
    add("CHECKS")
    for check in observation.checks:
        add(f"  {_GUTTER[check.status]}  {check.name:<18} {check.equation}")
        if check.status != invariants.PASS:
            add(f"          {check.detail}")
            if check.expected is not None and check.actual is not None:
                add(f"          expected {check.expected}  |  actual {check.actual}")
            elif check.actual is not None:
                add(f"          observed {check.actual}")

    add("")
    add("FIGURES -- what may be published, and the check backing it (PUB-01/PUB-02)")
    for figure in invariants.FIGURES:
        try:
            value, backs = publisher.figure(figure)
            add(f"    {figure:<16} {_format_figure(figure, value)}")
            add(f"    {'':<16} backed by: {', '.join(backs) if backs else '(no check named)'}")
        except publish.Withheld as exc:
            name, status, detail = exc.reasons[0]
            add(f"    {figure:<16} withheld -- {name} ({status})")
            add(f"    {'':<16} {detail}")

    return "\n".join(out)

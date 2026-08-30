"""Rendering an observation for a human.

ARCHITECTURE.md sec.4: "every number displays the check that backs it, and a
failed check is rendered *louder* than a passing one." The webapp is not built,
but the rule is not a webapp rule -- it applies to the first surface that
displays a number, which is this one.

So: passing checks are indented and quiet, failures carry a `!!` gutter and
their detail, and the report ends with what may be published rather than with a
total. The last line of a report about fee routing should be about permission
to speak, not about a number.
"""

from __future__ import annotations

from . import invariants

_GUTTER = {invariants.PASS: "  ok  ", invariants.FAIL: "!!FAIL", invariants.UNCHECKED: "  --  "}


def _lamports(value) -> str:
    if value is None:
        return "unknown"
    return f"{value / 1_000_000_000:.9f} SOL ({value} lamports)"


def render(observation) -> str:
    out = []
    add = out.append

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
    add(f"    SEAL {split.seal:>5}    BURN {split.burn:>5}    OPS {split.paid:>5}")
    for attribution in split.attributions:
        add("")
        add(f"    {attribution.bps:>5} bps  {attribution.leg.upper():<5} {attribution.address}")
        add(f"              {'keyless' if attribution.keyless else 'a key can exist'}: {attribution.reason}")
    if observation.seal_balances:
        add("")
        for address, lamports in observation.seal_balances.items():
            add(f"    balance   {address}")
            add(f"              {_lamports(lamports)}  (observed, NOT reconciled -- see SEAL_BALANCE)")

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

    verdict = observation.verdict
    add("")
    add("THE SILENCE RULE")
    if verdict.publishable:
        add(f"    may publish:  {', '.join(sorted(verdict.publishable))}")
    else:
        add("    may publish:  nothing")
    for figure, reasons in sorted(verdict.blocked.items()):
        name, status, detail = reasons[0]
        add(f"    withheld:     {figure} -- {name} ({status})")
        add(f"                  {detail}")
    return "\n".join(out)

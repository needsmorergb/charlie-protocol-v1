"""The publication boundary (PUB-01/PUB-02): the single accessor every
surface obtains a figure through.

`invariants.apply_silence_rule()` already computes which figures are
publishable and why the rest are withheld; that boundary computation stays
there. This module is the *gate* that makes bypassing it require deleting
code rather than forgetting a rule -- a direct read of an `Observation`
field for a figure's value, anywhere outside `FIGURE_SOURCES`, is exactly the
bypass PUB-01 forbids.

`FIGURE_SOURCES` is the only place in the codebase that knows how to turn an
`Observation` into a number a human might read. Every name in
`invariants.FIGURES` must appear here, or a figure has no source at all --
that is a bug in this module, not a silence the protocol intends.

`report.py` (the text surface) and `publish.public_record()` (the `--json`
surface, called from `cli.py`) both render every figure through
`Publisher.figure()` -- there is exactly one boundary, used by both.
"""

from __future__ import annotations

from . import invariants


def _split_value(observation):
    split = observation.split
    if split is None:
        return None
    return {"seal": split.seal, "burn": split.burn, "paid": split.paid}


def _leg_total(observation, leg: str):
    """Sum of recorded lamports across every destination of `leg` (`seal` or
    `paid`) in this observation's split -- `None` when there is nothing to
    sum (no evidence handle was consulted, or no destination of this leg
    exists in the split).
    """
    if observation.split is None or observation.evidence is None:
        return None
    destinations = [a.address for a in observation.split.attributions if a.leg == leg]
    if not destinations:
        return None
    total = 0
    seen = False
    for address in destinations:
        value = observation.evidence.get(address)
        if value is None:
            continue
        seen = True
        total += value
    return total if seen else None


def _seal_total(observation):
    return _leg_total(observation, "seal")


def _ops_total(observation):
    return _leg_total(observation, "paid")


def _burn_total(observation):
    """BURN_TOTAL: SOL spent at a BURN destination (PROTOCOL.md sec.4's
    `burn_spend` equation) -- distinct from SUPPLY_DESTROYED's token count.
    No coin has a BURN destination while `Registry.program_id` is `None`
    (the protocol program is not deployed), so this always resolves to
    `None` today; `invariants.burn_spend()` keeps `BURN_TOTAL` correctly
    `UNCHECKED` regardless of what this returns.
    """
    return None


def _supply_destroyed(observation):
    """D-09: every burn against the mint, by anyone -- the evidence store's
    own running total (`Evidence.total_burned`, surfaced onto the
    observation's `evidence` block as `"burn_total"` by `observe.py`).
    """
    if observation.evidence is None:
        return None
    return observation.evidence.get("burn_total")


FIGURE_SOURCES = {
    invariants.SPLIT: _split_value,
    invariants.SEAL_TOTAL: _seal_total,
    invariants.BURN_TOTAL: _burn_total,
    invariants.OPS_TOTAL: _ops_total,
    invariants.SUPPLY_DESTROYED: _supply_destroyed,
}


class Withheld(Exception):
    """Raised by `Publisher.figure()` when a figure's backing checks have
    not all passed. Carries the figure name and the blocking reasons
    (`(check_name, status, detail)` tuples, exactly `Verdict.blocked`'s
    shape) so a caller can render *why* without recomputing the silence rule
    itself.
    """

    def __init__(self, figure: str, reasons):
        self.figure = figure
        self.reasons = list(reasons)
        detail = "; ".join(f"{name} ({status}): {detail}" for name, status, detail in self.reasons)
        super().__init__(f"{figure} is withheld -- {detail}")


class Publisher:
    """The single accessor every surface obtains a figure through.

    Wraps one `Observation`. `apply_silence_rule()`'s verdict (already
    computed onto `observation.verdict` by `observe.observe()`) decides
    publishability; this class is only the gate over it.
    """

    def __init__(self, observation):
        self.observation = observation
        self.verdict = observation.verdict

    def figure(self, name: str):
        """`(value, backing_check_names)` when `name` is publishable.

        Raises `Withheld` otherwise, carrying the blocking check names and
        their statuses/details -- exactly what stopped it.
        """
        if self.verdict is None or not self.verdict.may_publish(name):
            reasons = (self.verdict.blocked.get(name) if self.verdict is not None else None) or [
                ("NO_CHECK", invariants.UNCHECKED, "no observation to check this figure against")
            ]
            raise Withheld(name, reasons)
        source = FIGURE_SOURCES.get(name)
        value = source(self.observation) if source is not None else None
        backing = tuple(
            check.name
            for check in self.observation.checks
            if name in check.backs and check.passed
        )
        return value, backing

    def publishable(self) -> frozenset:
        return self.verdict.publishable if self.verdict is not None else frozenset()

    def withheld(self) -> dict:
        return dict(self.verdict.blocked) if self.verdict is not None else {}


def public_record(observation) -> dict:
    """The one JSON shape a human-facing surface may emit -- the `--json`
    console path's counterpart to `report.render()`'s text (PUB-01/PUB-02).

    Every figure in `invariants.FIGURES` is obtained through
    `Publisher.figure()`: present under `"figures"` with its value and
    backing check names when publishable, present under `"blocked"` (naming
    the check that stopped it, never the value) when withheld. Non-figure
    facts (mint, error, the raw check list) are unaffected -- the rule is
    about the names in `invariants.FIGURES`, not about every field an
    observation carries.
    """
    publisher = Publisher(observation)
    figures: dict = {}
    blocked: dict = {}
    for name in invariants.FIGURES:
        try:
            value, backs = publisher.figure(name)
            figures[name] = {"value": value, "backed_by": list(backs)}
        except Withheld as exc:
            blocked[name] = [
                {"check": check_name, "status": status, "detail": detail}
                for check_name, status, detail in exc.reasons
            ]
    return {
        "schema": observation.schema,
        "mint": observation.mint,
        "observed_at": observation.observed_at,
        "error": observation.error,
        "checks": [c.as_dict() for c in observation.checks],
        "figures": figures,
        "blocked": blocked,
    }

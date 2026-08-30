"""EVID-10: $CHARLIE's exact residual, recorded as an open discrepancy that
is correct *as of a named observation* -- not a one-time reconciliation.

Two corrections this module carries, both load-bearing (see
`.planning/phases/01-evidence/01-03-PLAN.md`'s "Two corrections this plan
carries" and `01-CONTEXT.md`'s `<specifics>`):

1. The quantity to explain is **40,045.536145 tokens** -- all non-boost
   burns, from `initial_supply` 1,000,000,000.000000 minus live supply
   956,384,474.035955 minus boost 43,575,480.427900. An earlier writeup
   quoted "~5,346" for the same underlying gap, measured from a different
   baseline (that figure minus the buildlog's ~34,700 estimate of known
   non-boost burns). The two are the same quantity from two starting points,
   not a contradiction -- `render()` says so and never describes them as
   inconsistent.
2. **The residual is moving.** Live supply read 956,389,829.73 three days
   before this was written and 956,384,474.035955 at writing -- 5,355.69
   tokens burned in between, long after the boost's 341-second window
   closed. So this is not a one-time reconciliation: `reconcile()` returns a
   figure that is correct *as of the observation it carries*, and a later
   observation legitimately showing a different residual is expected
   behaviour for a coin whose holders keep burning, not evidence of an
   error.

All arithmetic here is on integer raw units -- UI values are formatted only
at `render()`'s boundary, and never fed back into a comparison.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

# The committed file (D-03/D-04's rule extended: the working evidence.db is
# gitignored, this rendered text is what ships). A function of the store --
# the same store reproduces the same file, always.
DEFAULT_OUTPUT_PATH = Path("state") / "RECONCILIATION.md"


def reconcile(evidence, mint: str, mint_state, *, observed_at: int | None = None) -> dict:
    """`initial_supply`, `live_supply`, `implied_total_burned`,
    `attributed_burned` (split by `burn_event.source`), and `residual` --
    computed only from stored evidence plus the passed-in supply reading
    (`mint_state`, itself an observation of the chain, never recomputed
    independently by this function).

    Every returned quantity is an exact `int` in raw units; no float appears
    anywhere in this computation.

    Returns a dict carrying the observation this reconciliation is true *as
    of*: `observed_at`, `burn_cursor_signature`, and `walk_complete`. With an
    underivable `initial_supply`, `residual` (and `implied_total_burned`) are
    `None` and `reason` names why -- never a residual computed against a
    guessed baseline. With an incomplete burn walk, a residual IS returned,
    but `walk_complete` is `False` -- the caller must never read that as a
    settled figure.
    """
    observed_at = int(observed_at) if observed_at is not None else int(time.time())
    live_supply = int(mint_state.supply)
    decimals = int(mint_state.decimals)

    burn_rows = evidence.burns_for(mint)
    attributed_by_source: dict[str, int] = {}
    for row in burn_rows:
        source = row.get("source") or "unknown"
        attributed_by_source[source] = attributed_by_source.get(source, 0) + int(row["tokens_burned"])
    attributed_burned = sum(attributed_by_source.values())

    walk_complete = evidence.is_backfill_complete(mint, "burn")
    burn_cursor = evidence.get_cursor(mint, "burn") or {}
    burn_cursor_signature = burn_cursor.get("last_signature")

    initial_supply_row = evidence.initial_supply_for(mint)
    if initial_supply_row is None or initial_supply_row.get("raw_supply") is None:
        reason = (
            (initial_supply_row or {}).get("unchecked_reason")
            or "initial_supply has not been derived for this mint yet"
        )
        return {
            "mint": mint,
            "observed_at": observed_at,
            "decimals": decimals,
            "initial_supply": None,
            "live_supply": live_supply,
            "implied_total_burned": None,
            "attributed_burned": attributed_burned,
            "attributed_burned_by_source": attributed_by_source,
            "residual": None,
            "reason": reason,
            "burn_cursor_signature": burn_cursor_signature,
            "walk_complete": walk_complete,
        }

    initial_supply = int(initial_supply_row["raw_supply"])
    implied_total_burned = initial_supply - live_supply
    residual = implied_total_burned - attributed_burned

    return {
        "mint": mint,
        "observed_at": observed_at,
        "decimals": decimals,
        "initial_supply": initial_supply,
        "live_supply": live_supply,
        "implied_total_burned": implied_total_burned,
        "attributed_burned": attributed_burned,
        "attributed_burned_by_source": attributed_by_source,
        "residual": residual,
        "reason": None,
        "burn_cursor_signature": burn_cursor_signature,
        "walk_complete": walk_complete,
    }


def record(evidence, result: dict, *, note: str | None = None) -> dict:
    """Append `result` (a `reconcile()` return value) as a new `discrepancy`
    row. Append-only, always: a second reconciliation of the same mint with
    a different `live_supply` produces a second row, and neither row is ever
    described as superseding the other -- two independent observations of a
    figure that moves.
    """
    by_source = result.get("attributed_burned_by_source") or {}
    boost = by_source.get("boost_buy_and_burn", 0)
    non_boost = result["attributed_burned"] - boost

    row_id = evidence.record_discrepancy(
        mint=result["mint"],
        observed_at=result["observed_at"],
        initial_supply=result["initial_supply"],
        live_supply=result["live_supply"],
        implied_total_burned=result["implied_total_burned"],
        attributed_burned=result["attributed_burned"],
        attributed_boost=boost,
        attributed_non_boost=non_boost,
        residual=result["residual"],
        decimals=result["decimals"],
        burn_cursor_signature=result["burn_cursor_signature"],
        walk_complete=result["walk_complete"],
        note=note if note is not None else result.get("reason"),
    )
    return evidence.discrepancies_for(result["mint"])[-1] if row_id else None


def _ui(raw: int, decimals: int) -> str:
    return f"{raw / (10 ** decimals):,.{decimals}f}"


def render(row: dict) -> str:
    """Render a *stored* `discrepancy` row. Rendering the same row twice
    produces identical text, always -- this reads only the row's own
    fields, never the current time or a fresh chain read.
    """
    out = []
    add = out.append
    decimals = row.get("decimals", 6)

    stamp = datetime.fromtimestamp(row["observed_at"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    add(f"mint                   {row['mint']}")
    add(f"observed at            {stamp}  (epoch {row['observed_at']})")
    add(f"burn cursor            {row.get('burn_cursor_signature') or 'none recorded yet'}")
    add(f"burn walk              {'complete' if row.get('walk_complete') else 'INCOMPLETE'}")
    add("")

    if row.get("residual") is None:
        add("RESIDUAL: not computable")
        add(f"    {row.get('note') or 'initial_supply is underivable for this mint'}")
        return "\n".join(out)

    add("THE FIGURE TO EXPLAIN")
    add(f"    initial_supply         {row['initial_supply']:>18,}  raw units  ({_ui(row['initial_supply'], decimals)})")
    add(f"    live_supply            {row['live_supply']:>18,}  raw units  ({_ui(row['live_supply'], decimals)})")
    add(f"    implied_total_burned   {row['implied_total_burned']:>18,}  raw units  ({_ui(row['implied_total_burned'], decimals)})  "
        "(initial_supply - live_supply)")
    add(f"    attributed_burned      {row['attributed_burned']:>18,}  raw units  ({_ui(row['attributed_burned'], decimals)})  "
        "(every burn this scan has recorded so far)")
    add(f"        boost              {row.get('attributed_boost', 0):>18,}  raw units  ({_ui(row.get('attributed_boost', 0), decimals)})  "
        "(pump's boost authority, at migration -- supply destroyed, not protocol-attributed, D-11)")
    add(f"        non-boost          {row.get('attributed_non_boost', 0):>18,}  raw units  ({_ui(row.get('attributed_non_boost', 0), decimals)})  "
        "(every other recorded burn -- a stranger burning their own tokens still counts, D-09)")
    add(f"    residual               {row['residual']:>18,}  raw units  ({_ui(row['residual'], decimals)})  "
        "(implied_total_burned - attributed_burned)")
    add("")
    add("This residual is correct AS OF the observation above -- not a fixed historical")
    add("gap. $CHARLIE's supply is still falling: a coin whose holders keep burning")
    add("tokens directly, well after any one-shot event, will show a different residual")
    add("at the next observation. That is the expected behaviour of an actively-burning")
    add("coin, not evidence of an error, and no future observation supersedes this one --")
    add("both remain readable as what was true at their own moment.")
    add("")
    add("If you have seen a smaller figure for this same gap quoted elsewhere, it is the")
    add("same quantity measured from a different, earlier baseline -- subtracting that")
    add("baseline's own estimate of already-known non-boost burns from the residual above")
    add("reproduces it. The two are not in conflict; they are two measurements of the same")
    add("thing from two different starting points.")
    if not row.get("walk_complete"):
        add("")
        add("The mint-wide burn walk is INCOMPLETE -- this residual is not yet fully")
        add("attributable to a scanned burn history, and must not be read as a settled figure.")
    if row.get("note"):
        add("")
        add(row["note"])
    return "\n".join(out)

# charlie_protocol

Charlie Protocol — a fee-routing and **verification** standard for pump.fun
coins.

Three legs (SEAL, BURN, OPS), a set of invariants, and — the part nobody else
specifies — what a coin is permitted to *claim* about its fees in public.

- [**PROTOCOL.md**](PROTOCOL.md) — the spec: legs, modes, invariants, claims policy
- [**ARCHITECTURE.md**](ARCHITECTURE.md) — what goes on-chain, what does not, and why
- [**BUILDLOG.md**](BUILDLOG.md) — build-in-public log, append-only
- `indexer/` — the code, and the executable statement of the spec

Spec and implementation are in one repository on purpose. Every check in
`indexer/invariants.py` names the rule it enforces, what it expected and what it
found, so a claim in `PROTOCOL.md` and the code that tests it can be read side
by side.

## Status

Stated plainly, because a project whose product is "every number ships with a
check that can fail" cannot open by overstating itself.

| | State |
|---|---|
| `indexer/` — reads sharing configs, runs the checks | **built** · 172 offline tests |
| `program/` — `init_vault`, `crank_burn` | **not built** · no program id exists |
| `web/` — the index, `/coin`, `/enroll`, `/verify` | **not built** |

Of the nine checks the indexer knows about, `CONFIG_MINT`, `SPLIT_SUM`,
`SEAL_UNSPENDABLE`, `SEAL_BALANCE`, `BURN_SUPPLY`, `BURN_IRREVERSIBLE` and
`OPS_ROUTED` compute real `PASS`/`FAIL` against stored evidence, not a
placeholder. `BURN_SPEND` stays `UNCHECKED` — by construction, not by
omission: it needs recorded fee claims at a BURN destination, and no coin has
a BURN destination while the protocol program is not deployed. `UNCHECKED`
is not a soft pass: it withholds its figure exactly as hard as `FAIL` does,
and every check states why in one sentence.

`BURN_ATOMIC` needs its own paragraph because it means two different things
at two different levels, and conflating them was a mistake this project made
once already, corrected on the record rather than quietly. At the
per-transaction level, `scan.classify_atomicity` classifies each of
$CHARLIE's 29 recorded boost-crank burns individually, and all 29 classify
`PASS`. That is not the same claim as the aggregate `BURN_ATOMIC` check
`observe()` reports: the mint-wide burn walk is incomplete
(`state/evidence/discrepancy.jsonl`'s `walk_complete: 0`), so no aggregate
atomicity verdict is claimed for $CHARLIE today. Separately, as of D-14,
`BURN_ATOMIC` is narrowed to gate only protocol-attributed burns (D-10) —
PROTOCOL.md sec.4's atomicity requirement is about the protocol's own BURN
leg, not third-party burns. $CHARLIE has zero protocol-attributed burns (no
protocol program exists yet), so `BURN_ATOMIC` reads not-applicable for
$CHARLIE, and for every coin, until phase 5.

$CHARLIE specifically: `SEAL_UNSPENDABLE` fails permanently — its seal
address is a vanity address rather than the program-derived one PROTOCOL.md
sec.3 requires, and its config is `admin_revoked` so only pump could ever fix
that. No seal total is publishable for $CHARLIE, now or later. The opening-balance mechanism
(EVID-02) is built and tested but dormant on live data until dedicated PDA
vaults exist (phase 5) — every SEAL destination today is the grandfathered
shared address, which the mechanism deliberately excludes (D-06/D-07).
[`state/RECONCILIATION.md`](state/RECONCILIATION.md) is the committed,
reproducible record of $CHARLIE's exact residual, correct as of a named
observation.

The plan for closing that runs to five phases, and "done" for each one means a
check that currently reads `UNCHECKED` returns `PASS` or `FAIL` — not that code
exists. The public surface lands in phase 2, deliberately ahead of the deploy
gate rather than behind it. Phase 1 is complete; the program is phase 4 and is
not written.

## Running the indexer

Python 3.11, standard library only. No install step, no dependency tree — a
verifier you have to build an environment for is a verifier fewer people will
ever run.

```bash
python -m indexer observe 8FhAXv2tfXUpyMbJsHDHX9zfiEb9PERzFWSY9sgLpump
```

`--store` appends the observation to `state/observations.jsonl`, `--json` gives
one record per line, `--rpc` (or `CHARLIE_RPC_URLS`) chooses endpoints.

```bash
python -m indexer log --mint <mint>          # replay the append-only record
python -m indexer derive <mint> --program X  # the vault PDAs for a coin
python -m indexer scan <mint> --evidence      # walk SEAL/OPS inflows and burns into evidence
python -m indexer reconcile <mint> --evidence --write   # EVID-10's residual, as of an observation
```

Exit codes: `0` every check that ran passed · `1` a check FAILED · `2` the coin
could not be observed at all.

## Tests

```bash
python -m unittest discover -s tests -t tests
```

Offline, no network. Every account fed to a decoder is built byte by byte, so a
layout change in pump's program surfaces as a failing decode rather than as a
wrong number in a published post.

## Relationship to the other repos

This repository is the whole of the public project. There is one implementation
and no second copy to drift.

- **`charlie_xbot`** — private. The $CHARLIE burn watcher, live since
  2026-08-23. Predates the protocol and implements one leg of it. Where the
  hand-rolled base58, ed25519, PDA derivation and transaction building came
  from.

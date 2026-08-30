# charlie_protocol

The implementation of [Charlie Protocol](https://github.com/needsmorergb/charlie-mode)
— a fee-routing and **verification** standard for pump.fun coins.

The spec lives in `charlie-mode`: `PROTOCOL.md` defines the rules, three legs
(SEAL, BURN, OPS), the invariants, and — the part nobody else specifies — what a
coin is permitted to *claim* about its fees in public. This repo is the code.

## Status

Stated plainly, because a project whose product is "every number ships with a
check that can fail" cannot open by overstating itself.

| | State |
|---|---|
| `indexer/` — reads sharing configs, runs the checks | **built** · 36 offline tests |
| `program/` — `init_vault`, `crank_burn` | **not built** · no program id exists |
| `web/` — the index, `/coin`, `/enroll`, `/verify` | **not built** |

Of the seven checks the indexer knows about, four compute and three are
`UNCHECKED` because the evidence they need is not recorded yet. `UNCHECKED` is
not a soft pass: it withholds its figure exactly as hard as `FAIL` does. So
today the indexer publishes a coin's **split** and nothing else.

[`ROADMAP.md`](ROADMAP.md) is the plan for closing that, in order, with what
"done" means for each phase.

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

- **[`charlie-mode`](https://github.com/needsmorergb/charlie-mode)** — public.
  The spec, the architecture, and the build log. It also still carries a copy of
  the indexer under `protocol/`; consolidating that is open question 3 in the
  roadmap.
- **`charlie_xbot`** — private. The $CHARLIE burn watcher, live since
  2026-08-23. Predates the protocol and implements one leg of it. Where the
  hand-rolled base58, ed25519, PDA derivation and transaction building came
  from.

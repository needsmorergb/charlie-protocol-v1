<!-- GSD:project-start source:PROJECT.md -->

## Project

**Charlie Protocol**

A fee-routing and **verification** standard for pump.fun coins, and the code
that implements it. It defines three destinations for creator fees — SEAL, BURN,
OPS — and the part nobody else specifies: what a coin is permitted to *claim*
about them in public. Routing is commodity; pump already ships it. The claims
policy and the invariants are the protocol.

It is for pump.fun operators who want a burn claim that survives being checked,
and for the researchers and traders who will check it.

**Core Value:** **Coins other than $CHARLIE enrol, and reconcile.** Everything else — the
checks, the index, the site — is either what makes enrolling worth something or
it is decoration.

The uncomfortable corollary, recorded here so it is never a surprise: a coin
cannot enrol until the program is deployed **and frozen**, because an
upgradeable program could add a withdraw instruction tomorrow, which reduces
SEAL from a proof back to a promise. The core value therefore sits behind a
mainnet deploy that is not yet funded. See Constraints.

### Constraints

- **Budget**: no SOL allocated for a mainnet deploy — phase 2 is gated at the deploy step. Everything through local-validator testing costs nothing; deploy, production cranks and the freeze do. A frozen program's rent is unrecoverable, so treat deploy cost as spent, not staked.
- **Timeline**: an X rollout thread is drafted for **2026-08-31, 09:30 ET**. Nothing built before then changes what is postable — the indexer backs a coin's split and nothing else, plus the red state on our own seal. The thread must claim exactly that.
- **Dependencies**: pump, read-only and permissionless. No step may require pump's cooperation, signature, or agreement.
- **Tech stack (indexer)**: Python 3.11, standard library only, no install step. A verifier you have to build an environment for is a verifier fewer people will ever run.
- **Tech stack (program)**: Rust / Anchor. Neither `solana` nor `anchor` is installed; native Windows is the hard path and WSL is the likely answer.
- **Immutability**: revoking upgrade authority is a one-way door that freezes every bug permanently. It happens only against a published checklist, never opportunistically.
- **Platform**: Windows. Known traps from `charlie_xbot` — `shutil.which` before `subprocess` for npm shims, `sys.stdout.reconfigure(encoding="utf-8")` for emoji, `cygpath -w` for paths through bash.
- **Editorial**: the silence rule governs the code, the site and the posts. One policy, three surfaces. If an invariant fails or was never computed, nothing gets published — not a corrected number, not a caveat.

<!-- GSD:project-end -->

<!-- GSD:stack-start source:STACK.md -->

## Technology Stack

Technology stack not yet documented. Will populate after codebase mapping or first phase.
<!-- GSD:stack-end -->

<!-- GSD:conventions-start source:CONVENTIONS.md -->

## Conventions

Conventions not yet established. Will populate as patterns emerge during development.
<!-- GSD:conventions-end -->

<!-- GSD:architecture-start source:ARCHITECTURE.md -->

## Architecture

Architecture not yet mapped. Follow existing patterns found in the codebase.
<!-- GSD:architecture-end -->

<!-- GSD:skills-start source:skills/ -->

## Project Skills

No project skills found. Add skills to any of: `.claude/skills/`, `.agents/skills/`, `.cursor/skills/`, `.github/skills/`, or `.codex/skills/` with a `SKILL.md` index file.
<!-- GSD:skills-end -->

<!-- GSD:workflow-start source:GSD defaults -->

## GSD Workflow Enforcement

Before using Edit, Write, or other file-changing tools, start work through a GSD command so planning artifacts and execution context stay in sync.

Use these entry points:

- `/gsd-quick` for small fixes, doc updates, and ad-hoc tasks
- `/gsd-debug` for investigation and bug fixing
- `/gsd-execute-phase` for planned phase work

Do not make direct repo edits outside a GSD workflow unless the user explicitly asks to bypass it.
<!-- GSD:workflow-end -->

<!-- GSD:profile-start -->

## Developer Profile

> Profile not yet configured. Run `/gsd-profile-user` to generate your developer profile.
> This section is managed by `generate-claude-profile` -- do not edit manually.
<!-- GSD:profile-end -->

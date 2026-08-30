# Roadmap

`ARCHITECTURE.md` names three pieces: one small program, one indexer, one
webapp. This file orders them, says what each has to be true for before it is
finished, and names the questions that are still open.

**"Done" is not "the code exists."** For every phase below, done is defined as
a check moving out of `UNCHECKED` — because a figure this protocol publishes is
a figure some equation could have refused. Code that ships without moving a
check has not finished anything the protocol measures.

---

## 0. Where this stands

| | State |
|---|---|
| Spec — `PROTOCOL.md`, `ARCHITECTURE.md` | published `2026-08-29` |
| Indexer — reads configs, runs checks | **built**, 36 offline tests |
| Program — `init_vault`, `crank_burn` | **not built**, no program id exists |
| Webapp — index, `/coin`, `/enroll`, `/verify` | **not built** |

Seven checks exist. Four compute, three cannot:

| Check | Backs | Today |
|---|---|---|
| `CONFIG_MINT` | everything | computed |
| `SPLIT_SUM` | the split | computed |
| `SEAL_UNSPENDABLE` | the seal total | computed — **fails on $CHARLIE** |
| `BURN_IRREVERSIBLE` | the burn total | computed |
| `SEAL_BALANCE` | the seal total | `UNCHECKED` — no inflow recording |
| `BURN_SUPPLY` | the burn total | `UNCHECKED` — no burn events |
| `OPS_ROUTED` | the ops total | `UNCHECKED` — no inflow recording |

So the indexer publishes a coin's **split** and nothing else. Three of the four
figures the protocol exists to produce are withheld by its own silence rule.
That is the hole this roadmap closes, and it is why the indexer comes before
anything with a deploy key or a domain name attached to it.

---

## 1. The order, and why it is this order

**Indexer → program → webapp.**

The program is the load-bearing piece, and it is a **one-way door**: revoking
upgrade authority is what turns SEAL from a promise into a proof, and it also
freezes every bug in it permanently. You do not walk through a one-way door
holding an instrument you have not calibrated. The indexer is that instrument —
it is how anyone, us included, learns whether a crank did what it said. Finish
it first, and the program's test plan gets to be *"the indexer watched it and
the checks passed"* rather than *"we read the code and it looked right."*

The webapp comes last because it renders indexer output and nothing else. Built
today it would be a site whose every page said `UNCHECKED` — an honest screen,
and a pointless one.

---

## 2. Phase 1 — the evidence layer

Turn the three `UNCHECKED` checks into checks that can fail. No new
dependencies; the indexer stays standard library only, because a verifier you
have to build an environment for is a verifier fewer people will ever run.

### 1.1 Inflows, recorded per signature

Record every lamport arriving at a SEAL or OPS destination as an `inflow` row —
signature, leg, lamports, block time — derived from `getSignaturesForAddress`
plus the pre/post balance delta of each transaction.

**Two things this will hit, both already known:**

- **RPC pruning.** Public endpoints drop history. `charlie_xbot` found a
  17.37 SOL gap on exactly this and solved it by capturing an *opening balance*
  once, from the first visible transaction's pre-balance. The same trick works
  here and the same caveat applies: an opening balance is an admission that
  history was unreadable, so it is stored as its own field and never folded
  silently into the recorded total.
- **Shared destinations.** `burn111…111` receives from many creators, so a
  per-coin `==` reconciliation is arithmetically impossible there and the
  invariant degrades to `<=` (`PROTOCOL.md` §3). The code must carry that
  distinction per destination, not per run.

**Done when:** `SEAL_BALANCE` and `OPS_ROUTED` return `PASS` or `FAIL` on a real
coin, and a deliberately corrupted inflow log makes them `FAIL`.

### 1.2 Burn events

Record each burn as a `burn_event` — signature, SOL spent, tokens burned, supply
after. Two sources, and the second is the one a naive watcher already missed
once:

- SPL `burn` / `burnChecked` instructions against the mint;
- pump's `BoostBuyAndBurnEvent`, discriminator `3f451c16305cc2b9`, decoded from
  the `Program data:` logs. Filter on the discriminator or you pick up
  `SwapEvent` and get garbage. On $CHARLIE this is 29 cranks over 341 seconds at
  migration and **all but ~34.7k of every token ever burned**.

  Boost is read here, never invoked. It fires once at migration under pump's own
  authority, and a burn we cannot cause is still a burn we have to account for —
  a watcher that misses the largest event in a coin's history is the failure this
  phase exists to fix.

**Done when:** `BURN_SUPPLY` returns `PASS` or `FAIL`, and the $CHARLIE
arithmetic either closes or is published as an open discrepancy with its exact
size named.

### 1.3 The initial supply problem

`initial_supply − Σ burns == supply` needs an `initial_supply` that is **read,
not assumed**. Pump's standard is 1,000,000,000 and $CHARLIE matches it, but
"the number everyone uses" is exactly the kind of input this protocol refuses.
Derive it from mint history; where it cannot be derived, `BURN_SUPPLY` stays
`UNCHECKED` for that coin and says why.

This is where $CHARLIE's **5,346 unexplained tokens** live: supply reads
956,384,474.035955, boost accounts for 43,575,480, and the residue was recorded
as "~34,700". Either 1.2 closes that gap or it gets published as a gap.

### 1.4 `BURN_ATOMIC` — a check the spec requires and nothing computes

`PROTOCOL.md` §4 requires the swap and the burn to be instructions in the same
transaction. Nothing verifies it. Given a burn event's signature this is a
direct read: fetch the transaction, assert the swap and the `burn` share it. Add
it as an eighth check backing `BURN_TOTAL`.

### 1.5 Coverage — every pump coin with a sharing config

The index is only worth reading because it measures coins that never enrolled.
Enumerate sharing configs from `pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ`
(`getProgramAccounts`, discriminator + size filter, paged), then observe on a
cadence. Needs a coin table, per-coin tick scheduling, and honest handling of
the fact that public RPCs will rate-limit a full sweep.

### 1.6 Claims capture — deliberately last

`UNVERIFIED` is the only classification that is an accusation, and it rests on
scraped marketing copy rather than arithmetic. `ARCHITECTURE.md` §6 already
names the requirements: exact quote stored, source URL, timestamp, visible
correction path, and a bias to `NO_CLAIM` whenever the reading is ambiguous.

**This ships last or not at all** — see open question 4. Everything else in the
index works without it.

---

## 3. Phase 2 — the program

**Nothing in this phase waits on pump.** Pump is a fee source and a set of
public accounts to decode; it is not a counterparty. We do not ask it for
anything, do not need it to agree to anything, and no step below is blocked on a
conversation with anyone who works there.

That settles what was the largest open question in the build. Pump's boost vault
could in principle do the buying and burning for us — but `boost_buy_and_burn`
is signed by pump's boost authority, so routing the BURN leg through it means
depending on pump to crank on a cadence, which means asking. **The crank is
ours.** A protocol whose whole claim is permissionlessness cannot have a
permission request on its critical path.

### 2.0 Toolchain

This machine has `rustc` 1.97 and `cargo`. It has **no `solana` and no
`anchor`.** Native Windows is the hard path for the Solana toolchain; WSL is the
normal one. First task of the phase is an installed, verified toolchain that can
build and run a local validator — before a line of program code.

### 2.1 `init_vault(mint)`

Creates `seal_vault = PDA(["seal", mint])` and `burn_pool = PDA(["burn", mint])`.
Both become ordinary pump `update_fee_shares_v2` shareholders — pump pays native
SOL straight into them.

The guarantee is an **absence**: there is no instruction in this program that
moves lamports out of a `seal` PDA. That is verifiable by reading the program,
and only meaningful once the upgrade authority is gone (2.5).

### 2.2 `crank_burn(mint)` — permissionless, so assume an adversary calls it

Because one will. `ARCHITECTURE.md` §2's five defences, restated as acceptance
criteria:

| Defence | Why | Test |
|---|---|---|
| Fixed lots (0.05 SOL) | one crank cannot move the market far enough to be worth attacking | a crank requesting a larger size is rejected |
| In-program slippage bound | a caller who cannot choose the bound cannot widen it | caller-supplied minimum-out is ignored or refused |
| Per-coin minimum interval | the pool cannot be drained in one block during a manufactured dislocation | a second crank inside the cooldown fails |
| Capped reimbursement | uncapped repayment is extraction wearing a helpful hat | reimbursement above the bps cap fails |
| Burn in the same instruction | tokens never rest anywhere they could be diverted | no intermediate token account holds a balance across instructions |

### 2.3 The venue question — open

`crank_burn` has to buy the token somewhere, and *where* changes with the coin's
lifecycle: pre-graduation it is pump's bonding curve, post-graduation it is
`pump_amm`, and some coins end up elsewhere again. The program must either
handle both venues or declare a scope and refuse coins outside it. **Refusing
loudly beats a silent wrong-venue swap**, and this must close before the freeze,
because afterwards it cannot be changed.

### 2.4 Testing against real state

Devnet has no pump. So: a local validator with mainnet accounts cloned in (pump,
`pump_amm`, the fee-sharing program, a real coin's curve and config), plus
`simulateTransaction` against mainnet for encoding — which is how
`charlie_xbot`'s hand-rolled transaction builder was validated. Costs nothing,
broadcasts nothing.

### 2.5 The freeze gate

Deploy **upgradeable**. Run real cranks against a live coin. Freeze only when:

- [ ] the program is small enough to have been read completely, by someone else too;
- [ ] `crank_burn` has run in production under the upgradeable build, repeatedly;
- [ ] the indexer reconciles every one of those cranks — `BURN_SUPPLY` and
      `BURN_ATOMIC` both `PASS`;
- [ ] every adversarial test in 2.2 passes against the deployed build, not just
      locally;
- [ ] the venue question (2.3) is closed;
- [ ] the resulting program hash is published, so anyone can diff what was frozen
      against what was audited.

Then revoke upgrade authority. Not before, and there is no after.

### 2.6 Registry wiring

`indexer/legs.py` holds `PROGRAM_ID = None` on purpose: until the program is
deployed, no address derives as a seal or burn PDA, so every enrolled-looking
split correctly reads as OPS. Setting that constant is the last step of phase 2,
and the first moment `SEAL` can be attributed to anything but the grandfathered
address.

---

## 4. Phase 3 — the webapp

### 3.1 Stack

Default to a **static site generated from the observation log**, with the raw
JSON published beside every page. The protocol's claim is that anyone can
recompute its numbers; shipping the inputs next to the rendering is that claim
in a form a reader can act on. A server is a later decision, not a starting one.

### 3.2 Routes

```
/                 the index — every coin, its split, its class
/coin/<mint>      one coin: live checks, totals, links to the proof
/enroll           derive the two PDAs, emit the exact bps config to set
/verify/<mint>    paste a mint, get a shareable verdict
```

### 3.3 The rendering rules — these are the product, not the styling

- Every figure displays the check backing it. A figure with no passing check is
  not rendered as a figure at all.
- A failed check renders **louder** than a passing one.
- `UNCHECKED` is visually distinct from both `FAIL` and `PASS`, and it withholds
  its figure exactly as hard as `FAIL` does.
- $CHARLIE's own page shows `SEAL_UNSPENDABLE: FAIL` and publishes no seal
  total. The reference implementation displays its own red state, or the site is
  marketing.

### 3.4 `/enroll` and `/verify`

`/enroll` never takes custody and never asks for a key: it derives the PDAs for
a mint and shows the operator the exact config to set themselves. `/verify` is
the growth surface — one mint in, one verdict out, no account.

---

## 5. Rules that hold across all three

- **The indexer stays dependency-free.** Standard library, Python 3.11, no
  install step.
- **The log is append-only.** Corrections are new records, never edits — the same
  rule `BUILDLOG.md` follows.
- **Observation records are versioned** (`SCHEMA`). Every phase here adds fields;
  a reader of an old record must not mistake a missing field for a zero.
- **Failures are stored.** A red state is as durable and as linkable as a green
  one.
- **The silence rule applies to the site and the posts**, not just the indexer.
  One policy, three surfaces.

---

## 6. Open questions

1. **Which venue does `crank_burn` swap on** (2.3), and does it refuse coins
   outside that scope? Must close before the freeze.
2. **May a grandfathered on-curve address ever carry a seal total?** The code
   says no, so $CHARLIE's 178.7 SOL is unpublishable. Note that this is now
   **permanent**, not pending: the config is `admin_revoked`, only pump could
   reset it, and we are not asking. $CHARLIE stays mode 1 by force and publishes
   no seal total, for good. Changing that is a spec edit, not a code change.
3. **Repo relationship.** `charlie_mode` (public — spec, buildlog) still holds a
   copy of the indexer under `protocol/`. This repo now holds the working copy.
   Two copies is a divergence waiting to happen: either `charlie_mode/protocol`
   becomes a pointer here, or this repo absorbs the spec. Left open deliberately,
   because one of those repos is public and already has readers.
4. **Does `UNVERIFIED` ship at all?** (1.6) It is the only output that is an
   accusation and the only one built on text rather than arithmetic.
5. **Is the observation log committed?** This repo's `.gitignore` does not
   exclude `state/`, on the reasoning that an append-only record is worth more
   committed than local. It grows without bound; revisit before that matters.

---

## 7. Definition of done for this milestone

Not "three components exist". This:

- every check in §0 returns `PASS` or `FAIL` on a live coin, and none returns
  `UNCHECKED` for a reason that is "not built";
- the program is deployed, frozen, and its hash published;
- `PROGRAM_ID` is set, so a coin that enrolls is attributed as `SEAL` and `BURN`
  rather than read as OPS;
- a coin other than $CHARLIE has enrolled and reconciles;
- and the site shows a red state somewhere, truthfully.

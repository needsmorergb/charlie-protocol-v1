# Charlie Protocol — system design

Companion to `PROTOCOL.md`, which defines the rules. This defines the build.

---

## 1. What has to be on-chain, and why

Most of this system is not a smart contract, and pretending otherwise would be
theatre. The split:

| Concern | Where | Why |
|---|---|---|
| Routing fees to destinations | **pump** | `update_fee_shares_v2` already does it |
| SOL burn vault unspendability | **our program** | needs a PDA no key can sign for |
| Atomic buy-and-burn | **our program** | swap + burn must share one transaction |
| OPS payout | **nowhere** | it is an ordinary address in pump's config |
| Classification / index | **off-chain** | derived from public state, no trust needed |
| Invariant checking | **off-chain** | anyone can recompute it; that is the point |

So: **one small program, one indexer, one webapp.**

---

## 2. The program

Two instructions. The design story is that the guarantee comes from the
*absence* of code.

```
init_vault(mint)      -> creates the SOL burn vault PDA for a coin
crank_burn(mint)      -> atomic: pull -> swap -> SPL burn
```

**Vault PDAs**

```
SOL burn_vault = PDA(["SOL burn", mint])     receives the SOL burn leg
burn_pool  = PDA(["burn", mint])     receives the BURN leg
```

Both are pump `update_fee_shares_v2` shareholders — pump pays native SOL
straight into them.

**Why SOL burn is unspendable.** Not a promise, an absence. `SOL burn_vault` is a PDA
of this program, so only this program could ever sign for it. There is **no
instruction in this program that moves lamports out of a `SOL burn` PDA.** Anyone
can verify that by reading the program.

**This only holds if the program is immutable.** Upgrade authority MUST be
revoked before any coin enrols. An upgradeable program could add a withdraw
instruction tomorrow, which reduces SOL burn from a proof back to a promise. This
is the single load-bearing assumption in the entire protocol.

**`crank_burn` is permissionless** and must survive being called by an
adversary, because it will be:

- **Fixed lots.** Spend in fixed increments (0.05 SOL, Snowball's figure) so a
  single crank can never move the market far enough to be worth attacking.
- **In-program slippage bound.** Minimum tokens-out enforced on the CPI, not
  passed in by the caller. A caller who cannot choose the bound cannot widen it.
- **Minimum interval.** Per-coin cooldown, so the pool cannot be drained in one
  block during a manufactured price dislocation.
- **Capped reimbursement.** The caller is repaid actual transaction fee plus a
  small fixed tip, drawn from the BURN leg and capped in bps. Uncapped
  reimbursement is an extraction vector wearing a helpful hat.
- **Burn in the same instruction.** Tokens are burned before the transaction
  ends. They never exist at rest.

---

## 3. The indexer

Python. Reuses what `charlie_xbot` already has — `read_fee_share` already
decodes pump sharing configs, and the reconciliation logic already exists.

Per coin, per tick:

```
read sharing config      -> { SOL burn_bps, burn_bps, paid_bps }, admin_revoked
read SOL burn vault balance  -> SOL burn invariant
read mint supply         -> BURN invariant
read ops inflows         -> OPS routed total
recompute all invariants -> pass / fail
```

Writes an append-only record per observation. **Failures are stored, not
discarded** — a red state has to be as durable and as linkable as a green one,
or the index is marketing.

Scope: every pump coin with a sharing config, enrolled or not. Enrollment is
not required to be measured, and that is what makes the index worth reading.

---

## 4. The webapp

Two audiences, one dataset.

```
/                 the index — every coin, its split, its class
/coin/<mint>      one coin: live invariants, totals, links to the proof
/enroll           wizard: derive vaults, emit the exact config to set
/verify/<mint>    paste a mint, get a classification — shareable
```

**The rule that separates this from every burn dashboard:** every number
displays the check that backs it, and a failed check is rendered *louder* than
a passing one. Snowball's dashboard can only report good news, which is why it
is decoration. Ours has a red state and shows it.

**`/enroll`** never takes custody and never asks for a key. It derives the two
PDAs for a mint and shows the operator the exact bps config to set on their own
sharing config. They execute it themselves; the indexer notices and the coin
appears. Nothing is granted or approved.

**`/verify`** is the growth surface — one mint in, one shareable verdict out,
no account required.

---

## 5. Data model (sketch)

```
coin        mint, config, admin_revoked, first_seen
split       mint, SOL burn_bps, burn_bps, paid_bps, observed_at   (append-only)
inflow      mint, leg, signature, lamports, block_time
burn_event  mint, signature, sol_spent, tokens_burned, supply_after
check       mint, invariant, passed, expected, actual, observed_at
claim       mint, source_url, claim_text, status, evidence, reviewed_at
```

`split` is append-only because a coin changing its split is itself the story.

---

## 6. Open risks

**`UNVERIFIED` is an accusation built on scraped marketing copy.** The split is
arithmetic; whether a coin's public claims contradict it is a judgment about
text somebody wrote on a website. This is the one subjective input in the
system and the only one that can get us into a fight. It needs: the exact quote
stored, the source URL and timestamp, a visible correction path, and a bias
toward `NO_CLAIM` whenever the reading is genuinely ambiguous.

**Immutability is a one-way door.** Revoking upgrade authority means shipping
bugs permanently. The program must be small enough to audit completely, and
should be frozen only after the burn crank has run in production against a live
coin under an upgradeable build.

**Nothing here helps $CHARLIE.** Charlie's own config is `admin_revoked`, so it
cannot enrol in its own protocol without pump. It stays mode 1 by force, not by
choice, and the protocol should say so on Charlie's own page rather than let
someone else discover it.

# Charlie Protocol — specification (draft)

A fee-routing and **verification** standard for pump.fun coins.

The protocol defines three things: where creator fees go, who is allowed to
crank them, and — the part nobody else specifies — **what a coin is permitted
to claim about them in public.**

Routing is commodity. Pump ships it natively. The claims policy and the
invariants are the protocol.

---

## 1. Legs

Three destinations. They are not the same kind of object and the protocol
never calls them by the same word.

| | **BURN (SOL)** | **BURN** | **OPS** |
|---|---|---|---|
| Action | SOL → unspendable vault | SOL → buy token → SPL `burn` | SOL → spendable wallet |
| SOL supply | unchanged | unchanged | unchanged |
| Token supply | unchanged | **reduced** | unchanged |
| Keeper required | no | yes | no |
| Trust required | none | atomicity | **full** |
| Permitted claim | "burned", "deflationary" — **only if the destination passes `SOL_BURN_UNSPENDABLE`** | "burned", "permanently destroyed" | "funds operations" |
| Forbidden claim | ~~"burned"~~ when the destination is spendable | — | ~~"burned"~~, ~~"funds operations"~~ as a burn |

**Both legs burn.** Solana has no instruction that reduces SOL supply, so a
SOL burn is deflation: SOL sent where no key can ever spend it, which never
returns to circulation. A token BURN reduces
token supply outright.

**What the protocol enforces is the part everyone else asserts:**
that the destination is one SOL does not come back from. A burn claim is
permitted only where `SOL_BURN_UNSPENDABLE` passes, and two kinds of
destination pass it. The protocol's own destination is Solana's incinerator,
where the runtime removes credited lamports from the total supply. A
recognised burn address (`burn111…111` is one) also passes: the chain treats
it the way every burn address on every chain has been treated, SOL sent there
is out of circulation and stays there. A destination that is neither is an
address someone can spend from, and the check fails on it. The check does not
grade a coin against a protocol the coin is not in: the program-derived vault
standard of section 3 is for enrolled coins, and an unenrolled coin routing to
a recognised burn address is not failed for lacking a vault it was never
offered.

This binds us first, and it once bound us wrongly. $CHARLIE's `burn111…111`
is **on** the curve, and for four days this check failed our own coin for
that, grading it against the enrolled-coin standard. The check passes it now;
what still withholds $CHARLIE's SOL burn total is `SOL_BURN_BALANCE`, because
the address is shared and carries the weaker `<=` invariant (see the $CHARLIE
note in section 3). Both states were published on the live site rather than
excused. An OPS payment is never a burn under any circumstances: the wallet is
spendable by design.

---

## 2. Modes

All three modes are the same mechanism with a different split. One code path,
one audit.

**Every percentage is configurable.** Splits are expressed in bps, must sum to
exactly `10000`, and are set by the sharing config admin via
`update_fee_shares_v2`. They may be changed at any time — unless the config is
`admin_revoked`, in which case the split is permanent and only pump can reset
it.

### Mode 1 — SOL burn

```
{ SOL_BURN: 10000, BURN: 0, OPS: 0 }
```

100% to a provably unspendable vault. No keeper, no signing authority, no
cadence, no discretion, no MEV surface.

**Does:** makes it impossible for anyone — creator included — to ever touch the
fee stream.

**Does not:** reduce token supply, return value to holders, or reduce SOL
supply. Mode 1 is *evidentiary, not economic*. It is a commitment device. A
coin picks mode 1 to prove the dev cannot profit, and the protocol states
plainly that it does nothing else.

Reference implementation: **$CHARLIE**.

### Mode 2 — SPLIT

```
{ SOL_BURN: n, BURN: 10000 - n, OPS: 0 }
```

Divided between the SOL burn vault and a buy-and-burn leg that reduces token
supply. `n = 0` is legal — 100% BURN, the maximally deflationary configuration.

**Does:** converts fee revenue into supply reduction.

**Does not:** create a price floor. The BURN leg is funded by volume, so it
decays with volume. The protocol says this out loud.

No spendable wallet is in the path. Mode 2 is fully verifiable end to end.

### Mode 3 — OPERATED

```
{ SOL_BURN: n, BURN: m, OPS: k }     n + m + k = 10000
```

The only mode where a spendable wallet is in the fee path. Funds promotion,
gas, keeper costs, and anything else a live project actually needs.

**Does:** pay for the work.

**Does not:** carry a verifiable guarantee past the point of receipt. See §4.

The OPS destination MUST be disclosed at enrollment and SHOULD be a multisig,
not a single-signer wallet.

---

## 3. The SOL burn destination

The SOL burn destination is Solana's incinerator:

    1nc1nerator11111111111111111111111111111111

From the runtime's own source (`sdk/program/src/incinerator.rs`):

> Lamports credited to this address will be removed from the total supply
> (burned) at the end of the current block.

SOL routed there leaves the total supply. That is the runtime's guarantee
rather than ours: nothing is deployed, nothing is trusted, and no key exists
anywhere that could undo it.

This applies to lamports. Sending an **SPL token** to the incinerator does not
reduce its supply -- the runtime destroys lamports, not token balances. Token
supply is reduced through the token program's own burn instruction, which is
the BURN leg.

### Attribution

The incinerator is shared by the whole chain, so a coin's burn is attributed
per transaction, from the transfers into it, rather than from a balance. That
is exact rather than cumulative.

The balance is always zero, and the zero is the evidence. A vault holding the
SOL would prove only that the SOL is sitting there; a balance that stayed at
zero after lamports were credited proves they left the supply.

`SOL_BURN_BALANCE` therefore asks a different question of this destination
than of any other: **the balance must be zero.** A non-zero reading is the
anomaly, and a balance that could not be read is `UNCHECKED`, never a pass --
absence of a reading is not evidence of a burn.

### $CHARLIE

$CHARLIE routes to `burn111...111`, a shared vanity address that predates this
spec. Attribution across the coins sharing it is not possible, so it carries
the weaker `<=` invariant and the protocol publishes no SOL burn total for it.
New enrollments use the incinerator.

---

## 4. Invariants

The protocol's actual product. Every published figure is backed by a check
that **can fail**.

**SOL burn leg**

```
Σ recorded_inflows == getBalance(vault)           # dedicated PDA vault
opening + Σ recorded_inflows <= getBalance(vault) # legacy shared address
```

**BURN leg**

```
initial_supply − Σ burn_amounts == getMint(mint).supply
Σ SOL_spent_on_BURN <= Σ fees_claimed × bps_BURN / 10000
getMint(mint).mint_authority == None
```

Line 1 proves every claimed burn actually happened, read off the mint account
by anyone. Line 2 proves nothing was skimmed. Line 3 is why the first two mean
anything: a live mint authority can reissue every token a crank ever burned, so
without it "permanently destroyed" is false however honest the arithmetic is.

**OPS leg**

```
Σ routed_to_OPS == Σ protocol_inflows(ops_wallet)
```

This proves **how much was routed and nothing else.** Once SOL reaches a
spendable wallet the chain stops being evidence. The protocol makes no claim
about downstream use, does not audit it, and will not describe OPS spending as
verified. Mode 3 coins are verifiable up to the wallet boundary and no
further — that limit is published alongside the figure, not buried.

**Atomicity.** The swap and the burn MUST be instructions in the same
transaction. Both execute or neither does. Tokens never rest in an
intermediate wallet where they could be diverted.

**The silence rule.** If any invariant fails, the publisher does not post.
Not a corrected number, not a caveat — silence until it reconciles. A protocol
that can only ever report good news is reporting nothing.

An invariant that has **not been computed** withholds its figure exactly as
hard as one that failed. "The number is wrong" and "we do not know whether the
number is right" are the same statement to whoever reads the post. Every figure
names the check backing it; a figure no check backs is not a figure this
protocol publishes.

---

## 5. Cranking

Only the BURN leg needs a cranker. the SOL burn and OPS are pure routing.

1. **Permissionless, gas-reimbursed.** Anyone may crank. Atomicity means a
   caller cannot profit by misbehaving. Removes the operator from the trust
   equation entirely. *Preferred.*
2. **Pump's boost vault.** Fund it and pump's own authority does the buying and
   burning. Zero key on our side, but depends on pump agreeing to crank on a
   cadence — currently an open question.
3. **Operator keeper.** Full cadence control, requires a hot key for gas, puts
   the operator back in the trust equation. Fallback only.

---

## 6. Enrollment and classification

Permissionless. A coin enrolls by pointing fee shares at protocol
destinations; nothing is granted, approved, or gatekept.

Every coin — enrolled or not — is measured from public chain data alone, and
the report separates **the fact** from **the judgment**:

**The fact.** The split, in bps: `{ burn, sol_burn, paid }`. Derived from the
sharing config. Not an opinion.

**The judgment.** Whether the coin's public claims match that split:

| Status | Meaning |
|---|---|
| `VERIFIED` | claims match the chain |
| `NO_CLAIM` | makes no public claim about its fees |
| `UNVERIFIED` | claims a burn the chain does not support |

`UNVERIFIED` is the only class that is an accusation, and it is arithmetic.

**The badge.** "Charlie Protocol Verified" requires `VERIFIED` **and**
`paid ≤ 2500 bps`. Any split above that is a legal mode 3 configuration and is
reported accurately — it simply does not wear the badge. The protocol does not
gatekeep what a coin may do; it gatekeeps what may carry its name.

---

## 7. Non-goals

- Not a launchpad.
- Not a price floor. No mode creates one.
- Not custody. The protocol never holds a mint, a mint authority, or a
  spendable balance on behalf of an enrolled coin.
- Not an auditor of OPS spending. See §4.
- Not a moat. If pump ships this natively the protocol has succeeded.

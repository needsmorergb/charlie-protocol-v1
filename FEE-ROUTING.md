# Protocol-enforced fee routing — design

Companion to `ARCHITECTURE.md`. This is the design for a **mandatory** protocol
share: every coin using the protocol routes a fixed cut to buying and burning
$CHARLIE, whatever else its dev chooses to do with the rest.

Status: design. Nothing here is deployed, and the program it depends on is not
written.

---

## 1. The unit problem, first

"0.5% of every trade" cannot be expressed on pump, by us or by anyone.

pump's creator fee is itself a fraction of the trade, and a variable one:
roughly **0.05% to 0.95% of volume**, tiered by market cap. The sharing config
allocates **that fee** and nothing else -- a flat list of `(pubkey, bps)`
summing to 10000, where the bps are shares OF THE CREATOR FEE.

So at the low end, the entire creator fee is 0.05% of volume. A protocol cut of
"0.5% of every trade" would be ten times the whole fee. There is no instruction
that can take it, and no configuration that can express it.

What IS expressible is a share of the creator fee:

| Toll (bps of fee) | Share of the creator fee | Of trade volume, at 0.05% - 0.95% fee |
|---|---|---|
| 50 | 0.5% | 0.00025% - 0.00475% |
| 500 | 5% | 0.0025% - 0.0475% |
| 1000 | 10% | 0.005% - 0.095% |

**This is a decision, not a detail.** A competitor advertising "0.5% of every
trade" is either describing a share of the fee in loose language, or describing
something their own bot does with its trading, not something pump enforces.
The protocol states its cut in the unit pump actually uses, and says so.

`TOLL_BPS` below is written as 500 pending that decision.

---

## 2. Why pump cannot make it mandatory

The sharing config belongs to the coin's admin. They write the shareholder
list; nothing in pump lets a third party require an entry in it. A dev can
enrol, take the badge, then set a config without our address -- or never
include it at all.

So a cut written into pump's config is **certified, not enforced**: the site can
check it continuously and withdraw the classification when it stops being true.
That is real, and it is what the indexer already does for every other claim.
It is not "no questions asked".

The cut becomes unavoidable only if it is taken somewhere the dev does not
control, which means: **one destination on pump's side, and the split performed
by an immutable program.**

---

## 3. The shape

```
pump sharing config for an enrolled coin:

    collector(mint)   10000 bps        <- the whole creator fee, one entry

collector = PDA(["collect", mint])     no key exists for it
```

Then our program, and only our program, moves it:

```
distribute(mint)          permissionless, no signer

    L = collector lamports - rent exempt minimum

    L * TOLL_BPS   / 10000  -> charlie_pool       buy and burn $CHARLIE
    L * sol_burn   / 10000  -> incinerator        burned by the runtime
    L * own_burn   / 10000  -> burn_pool(mint)    buy and burn the coin
    L * ops        / 10000  -> route.ops_address  an ordinary wallet
```

`sol_burn + own_burn + ops == 10000 - TOLL_BPS`, enforced on write. The dev
picks those three freely, including setting two of them to zero. They cannot
pick the fourth, because it is not a field: **`TOLL_BPS` is a constant in the
program's code.**

The caller of `distribute` supplies no addresses. Every destination is derived
from the mint or read from the coin's own route account, so a caller cannot
redirect a lamport of it.

---

## 4. Accounts and instructions

```
route(mint)      = PDA(["route", mint])     authority, sol_burn, own_burn, ops,
                                            ops_address
collector(mint)  = PDA(["collect", mint])   receives the whole creator fee
burn_pool(mint)  = PDA(["burn", mint])      the coin's own buy-and-burn leg
charlie_pool     = PDA(["charlie"])         global, the toll accrues here
```

```
init_route(mint, sol_burn, own_burn, ops, ops_address)
set_route(mint, sol_burn, own_burn, ops, ops_address)
distribute(mint)                permissionless
crank_burn(mint)                atomic: pull -> swap -> SPL burn, the coin
crank_charlie_burn()            atomic: pull -> swap -> SPL burn, $CHARLIE
```

`init_route` and `set_route` require the signer to be the admin named by the
coin's own pump sharing config -- ownership is the key pump already recognises,
not a claim we invent.

**The guarantee is still an absence.** There is no instruction that moves
lamports out of `collector` except `distribute`, none that moves them out of
`charlie_pool` or `burn_pool` except the cranks, and none anywhere that sends
to a caller-supplied address. Anyone can read the program and confirm it, and
that only means anything while **upgrade authority is revoked** -- the single
load-bearing assumption, unchanged from `ARCHITECTURE.md`.

---

## 5. What this fixes, beyond the toll

pump allows a coin's split to be updated once (see the open question in
section 8). Under today's multi-shareholder shape that freezes a dev's chosen
ratios permanently, on their first and only attempt.

Under this shape the one-shot update is spent pointing at `collector`, which is
the same value for every coin and never needs to change again. The ratios move
into `route`, where `set_route` can change them any day, as often as the dev
likes. **The dev gets a permanent split on pump and a mutable one with us**,
which is strictly more freedom than they have now, and the toll survives every
change because it was never in the account they can write.

---

## 6. The recipient must exist, and the incinerator does not

Measured on mainnet through crowd-api on 2026-09-03:

```
1nc1nerator11111111111111111111111111111111
  state          UNINITIALIZED -- does not exist
```

That is not a glitch. The runtime removes lamports credited there at the end
of the block, so the account never carries a balance, and an account with no
lamports does not exist. It is the same fact `SOL_BURN_BALANCE` inverts to
"the balance MUST be zero", seen from the other side.

pump's on-chain IDL then says:

```
6070 UnableToDistributeCreatorFeesToUninitializedAccount
6052 UnableToDistributeCreatorFeesToExecutableRecipient
```

`distribute_creator_fees` pays every shareholder in ONE instruction. If pump
refuses an uninitialized recipient, then a config naming the incinerator
cannot be distributed AT ALL: not the burn share, and not the dev's share
either. The whole coin's fees would sit in the creator vault.

**This is measured for the account and inferred for the failure.** The
absence is certain; that 6070 fires on it has not been observed yet, and the
way to settle it is a simulation of `distribute_creator_fees` against a
config that names the incinerator. Nothing should ship, and no page should
promise this route, until that simulation has been run.

If it does fire, the collector shape in section 3 is not merely better, it is
the only one that works: pump only ever pays `collector(mint)`, an account we
create and keep rent exempt, and the incinerator is reached by an ordinary
system transfer from our own program, which burns exactly as it always has.

## 7. Sequencing, and what does NOT work before the program exists

PDAs derive from a program id, and a program id is a keypair we can generate
today and deploy to later. So `collector(mint)` is computable now.

That means enrolment can begin before the program is deployed: fees accrue at a
keyless address that **nobody can spend, including us**, and the moment the
immutable program is live at that id, `distribute` starts moving everything
that accumulated.

Two things break that plan, and section 6 is the reason. An unfunded
collector PDA does not exist either, so it is an uninitialized recipient like
the incinerator, and a distribution naming it would fail for the whole coin.
The collector must be created and rent exempt BEFORE a coin routes to it,
which needs the program, or at least a funded account at that address.

And the risk that remains, stated plainly because it is the kind of thing
this project exists to state: **if the program is never deployed, everything
routed to a collector is stranded forever.** Not stolen -- unspendable, by anyone, which is
exactly what makes it safe and also what makes it final. A dev's ops share is
in that vault too. Anyone enrolling before deployment is trusting that the
program ships, and the page must say so in those words.

Two honest options:

**Deploy first.** Write the program, revoke upgrade authority, create and
fund the collectors, then enrol. Enrolling into an address that does not yet
exist is not a trade-off any more, it is a configuration that cannot pay
anyone.

---

## 8. What the indexer and the site become

Simpler, not harder.

* **Enrolled** is now one check: does the coin's sharing config pay
  `collector(mint)` 10000 bps? Attribution across shareholders stops being
  necessary for enrolled coins.
* **A new state:** lamports sitting in `collector` are *collected, not yet
  distributed*. That is neither burned nor paid, and a page that shows it as
  either is lying. It gets its own row.
* **The figures** each come from a different place: SOL burned is what
  `distribute` sent to the incinerator, $CHARLIE burned is what
  `crank_charlie_burn` destroyed, the coin's own supply burned is
  `crank_burn`'s. All three are recomputable by a stranger from the program's
  own transfers.
* **Coins that route to the incinerator without enrolling** are still checkable
  and still get a verdict. `/verify` stays open to everyone; it is the growth
  surface, not a members' area.

---

## 9. Open questions this design does not settle

1. **`TOLL_BPS`.** 50, 500 or 1000 (section 1).
2. ~~Is pump's split update really one-shot?~~ **Settled, on chain.** The
   fee-share program's own IDL carries both instructions,
   `update_fee_shares` (`bd0d8863bba4ed23`) and `update_fee_shares_v2`
   (`6ffb31064e4e6a12`, the one this project calls), and error
   `6024 FeeSharesAlreadyUpdated - Reward split can only be updated once`.
   The warning on `/enroll` is the program's own words. pump's published docs
   IDL is simply older than the deployed program.
3. **Migration.** If the rule holds, coins enrolled under the current
   multi-shareholder split cannot be converted to a collector later. They keep
   their frozen split and their existing classification, and the protocol keeps
   two classes of coin permanently. Better to know that before onboarding
   anyone into the shape we intend to replace.
4. **`get_minimum_distributable_fee`.** pump enforces a floor before creator
   fees can be distributed at all. `distribute` must respect it, and the site
   must not describe a coin under the floor as failing.
5. **Does 6070 actually fire on the incinerator?** Section 6. This is the
   most urgent question in this document, because the answer decides whether
   the SOL burn leg works as `/enroll` currently configures it.

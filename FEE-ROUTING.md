# Protocol-enforced fee routing — design

Companion to `ARCHITECTURE.md`. This is the design for a **mandatory** protocol
share: every coin using the protocol routes a fixed cut to buying and burning
$CHARLIE, whatever else its dev chooses to do with the rest.

Status: design. Nothing here is deployed, and the program it depends on is not
written.

---

## 1. The unit problem, first

"0.5% of every trade" cannot be expressed on pump, by us or by anyone.

pump's creator fee is itself a fraction of the trade, and a variable one.
Read from the chain on 2026-09-04: a flat **30 bps** on the bonding curve, and
after graduation a 25-tier schedule that FALLS with market cap, **95 bps** at
420 SOL down to **5 bps** above 98,240 SOL. The 0.05% figure everyone quotes is
the large-cap rate, not the typical one. The sharing config
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

**Decided: `TOLL_BPS` is 1000**, ten percent of the creator fee. At 500 a coin
trading $1M/day sends about $25/day to the burn; at 50 a floor-sized
distribution yields less toll than the gas required to move it. See `BUILD.md`
section 3 for the arithmetic and the counterweight.

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

## 6. The incinerator has no account, and pump pays it anyway

Both halves measured on mainnet through crowd-api, 2026-09-03.

The account really does not exist:

```
1nc1nerator11111111111111111111111111111111
  state          UNINITIALIZED -- does not exist
```

It cannot. The runtime removes lamports credited there at the end of the
block, so the balance is always zero, and a zero balance account is no
account. That is `SOL_BURN_BALANCE`'s inverted invariant seen from the other
side.

pump's on-chain IDL carries `6070
UnableToDistributeCreatorFeesToUninitializedAccount`, and
`distribute_creator_fees` pays every shareholder in one instruction, so the
obvious reading was that a config naming the incinerator could never be
distributed at all, taking the dev's share down with the burn.

**That reading was wrong, and a simulation says so.** `5vxYBj3qbAFCSQr...pump`
routes 100% to the incinerator, and simulating `distribute_creator_fees`
against its live config succeeds: the system transfer executes, the program
returns success, and no error is raised. 6070 does not fire on an address the
system program owns; whatever it guards, this is not it.

So the SOL burn leg works exactly as `/enroll` already configures it, with no
program of ours involved: pump pays the incinerator directly, and the runtime
burns it at the end of the block. **419 sharing configs on mainnet already
name the incinerator first**, so this is a well-travelled route, not a
theory.

Two things the same run measured, both of which the crank has to respect:

* **A floor.** `Insufficient fees for distribution. Minimum vault balance
  needed: 1781760 lamports.` Below roughly 0.00178 SOL the instruction
  returns success WITHOUT distributing anything. A cranker that does not
  check first pays a fee to do nothing, and a page that reads that success
  as a distribution reports a burn that did not happen.
* **A recipient that is a program still fails**, per `6052`. A collector PDA
  is a data account and is fine; the program's own address is not.

## 7. Sequencing, and what does NOT work before the program exists

PDAs derive from a program id, and a program id is a keypair we can generate
today and deploy to later. So `collector(mint)` is computable now.

That means enrolment can begin before the program is deployed: fees accrue at a
keyless address that **nobody can spend, including us**, and the moment the
immutable program is live at that id, `distribute` starts moving everything
that accumulated.

Section 6 removes the objection that a collector must exist before it can be
paid: pump pays an address the system program owns whether or not it has ever
held a lamport. A collector PDA is not that, though. It is program owned, and
`6052` refuses an executable recipient, so the safe order is still to create
the account before routing to it rather than to assume.

The risk that remains is the one worth printing on the page: **if the program
is never deployed, everything routed to a collector is stranded forever.** Not stolen -- unspendable, by anyone, which is
exactly what makes it safe and also what makes it final. A dev's ops share is
in that vault too. Anyone enrolling before deployment is trusting that the
program ships, and the page must say so in those words.

Both halves of that turned out to be wrong, and measurement is what corrected
them.

An unfunded collector is NOT an ineligible recipient. Four recipient shapes
were simulated and scored on lamports moved: an ordinary wallet, a
non-executable PDA owned by pump, one owned by the fee-share program, and a
PDA that does not exist at all. All four were paid 49,189,376 lamports, and
the only refusal in the set was an executable account, at `6052`. So fees do
accrue at a collector that nothing has created yet.

And deploying frozen first is the wrong order anyway. The graduated-coin path
in `BUILD.md` section 8 was invisible until it was measured, and freezing
before it was found would have shipped a permanent bug on the highest-volume
coins. The order is: deploy upgradeable, run the whole pipeline in production
against a live coin including a graduated one, then revoke and publish a
reproducible build. Early enrollers are enrolling into an upgradeable program
and the page says so.

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

1. ~~`TOLL_BPS`.~~ **Decided: 1000.** Section 1, and `BUILD.md` section 3.
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
4. ~~`get_minimum_distributable_fee`.~~ **Settled, and it is not a floor.** It
   is a RETAINED RESERVE: `distributable = balance - minimum_required`, and
   pump pays out everything above it. The value was 810624 on 2026-09-03 and
   two other numbers earlier the same day, because it tracks the rent-exempt
   minimum. Read it per coin, never hardcode it, and never gate on
   `can_distribute`, which answered True beside `distributable_fees = 0`.
5. ~~Does 6070 fire on the incinerator?~~ **Settled. It fires on nothing.**
   The one refusal pump has is `6052`, for an executable recipient. Section 6.
6. ~~Can a dev escape the toll?~~ **Settled: no.** `reset_fee_sharing_config`
   refuses the config's own admin with `6016`, charged to pump's `global`
   account, and answers only to pump's `admin_set_creator_authority`;
   `transfer_` and `revoke_fee_sharing_authority` are dead at `6023` for
   everyone. Even a successful reset pays the accrued fees to the outgoing
   split. `BUILD.md` sections 1 and 11.
7. ~~Does an enrolled coin keep paying after it graduates?~~ **Only if the
   crank changes.** `Pool.coin_creator` is the sharing config on 343 of 343
   graduated coins, so the routing survives, but the money is wSOL in an AMM
   account that `distribute_creator_fees` cannot see: six coins paid zero
   alone and their full balances after `transfer_creator_fees_to_pump`.
   `BUILD.md` section 8.
8. **Cashback coins pay nothing on every leg** and are refused at enrolment.
   `BUILD.md` section 6.

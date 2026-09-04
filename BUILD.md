# Charlie Protocol — build specification

What to build, with every number taken from the chain rather than from a
document. Companion to `PROTOCOL.md` (the rules), `ARCHITECTURE.md` (the
system) and `FEE-ROUTING.md` (why the shape is what it is).

Every fact in section 1 was measured against mainnet through crowd-api on
2026-09-03, and every one of them is reproducible: the `trace`, `collector`,
`reset` and `graduated` workflows in the site repository re-run the tools that
produced them. Where a claim rests on one coin rather than several, or on a
simulation rather than a landed transaction, it says so.

---

## 1. Measured

### The instructions

| | |
|---|---|
| Fee-share program | `pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ` |
| `create_fee_sharing_config` | disc `c34e564c6f34fbd5`, no args, sole signer `payer` |
| `update_fee_shares_v2` | disc `6ffb31064e4e6a12`, signer `authority` |
| `distribute_creator_fees` | disc `a572670079cef751`, **no signer**, 7 accounts, no AMM accounts |
| `distribute_creator_fees_v2` | disc `ffcb134ff444089f`, signer `payer` |
| `pump_amm::transfer_creator_fees_to_pump` | disc `8b348655e4e56cf1`, **no signer**, no floor |
| `pump_amm::migrate_pool_coin_creator` | **no signer** |
| `transfer_fee_sharing_authority` | **dead**, `6023 DeprecatedInstruction`, for every caller |
| `revoke_fee_sharing_authority` | **dead**, `6023`, for every caller |

### The rules

| Fact | Evidence |
|---|---|
| A split may be updated exactly once | `6024 FeeSharesAlreadyUpdated - Reward split can only be updated once` |
| Spending the update sets `admin_revoked` | post-simulation config bytes after create + update in one transaction |
| Since revoke is dead, `admin_revoked` can ONLY mean the update was spent | `6023` for every caller of `revoke_fee_sharing_authority` |
| `create_fee_sharing_config` makes admin = the coin's creator | decoded `CreateFeeSharingConfigEvent` + post-state, three coins |
| A stranger cannot create a coin's config | `6016 NotAuthorized` |
| Creation leaves the one-shot unspent | create + `update_fee_shares_v2` in one transaction, simulated OK |
| Creation migrates `bonding_curve.creator` to the config PDA | curve bytes before and after, three coins |
| Remaining accounts must be the shareholders in config order | `6054` |
| The ONLY refused recipient is an **executable** account | `6052`; `6070` fired on nothing |
| A recipient that does not exist is paid | 0 → 49,189,376 lamports, measured |
| A non-executable PDA is paid, whoever owns it | 49,189,376 to PDAs owned by pump and by the fee-share program |
| The incinerator is paid | 0 → 19,843,224 lamports, reproduced across two runs |
| `Pool.coin_creator` is the sharing config after graduation | 343 of 343 graduated enrolled coins |
| Graduated fees are wSOL in an AMM account, invisible to `distribute_creator_fees` | six coins: 0 paid alone, full balance paid after the AMM transfer |
| ~3.2% of configs have graduated | 343 of 10,599 sampled |
| ~95% of fresh launches have no config at all | 41 of 43 sampled from the launch stream |
| A fresh launch that HAS a config already spent its one-shot | 10 of 10 sampled |

### The reset matrix

Only one address on Solana can reset a config, and it is not the dev:

| caller | `reset_fee_sharing_config` / `_v2` |
|---|---|
| the config's own admin | `6016 NotAuthorized`, **charged to `global`** |
| a stranger | `6016` |
| pump `Global.authority` | `6016` |
| pump `Global.set_creator_authority` | `6016` |
| pump `Global.admin_set_creator_authority` | **succeeds** |

A successful reset re-arms the one-shot and rewrites the shareholders, but it
pays accrued fees to the **outgoing** split first: the old shareholder received
49,189,376 lamports and the resetter received zero. Rests on one coin's complete
matrix; a second reproduced every refusal it reached before an RPC 429 ended
the run.

### The floor is a reserve, not a gate

`distributable_fees = balance − minimum_required`, and pump distributes
everything above it. `minimum_required` was **810,624** on 2026-09-03; earlier
runs saw 1,781,760 and 1,621,248. It tracks the rent-exempt minimum and it has
moved three times in one day.

**Read it per coin with `get_minimum_distributable_fee`. Never hardcode it, and
never gate on `can_distribute`** — that returned `True` next to
`distributable_fees = 0`. Only `distributable_fees` is load-bearing. The AMM
side retains its own reserve, 1,855,569 lamports in the vault ATA.

---

## 2. What needs a program, and what does not

The SOL burn leg needs no program of ours when the incinerator is a pump
shareholder directly — measured, and 419 configs already do it. Under the
collector shape below, pump no longer pays the incinerator; **we** do, and that
path has never been measured because the program does not exist. It is the one
thing on the devnet checklist in section 8.

The program exists for three things:

1. **The $CHARLIE toll**, unavoidable only if taken where the dev cannot reach.
2. **Buy-and-burn**, because SOL cannot destroy a token without a swap.
3. **A split the dev can change**, since pump's is one-shot and permanent.

---

## 3. Constants

```
TOLL_BPS = 1000                 10% of the creator fee
```

Ten percent of a fee that is 0.05% to 0.95% of trade volume, tiered inversely
to market cap, so a coin doing real volume sits near the low end. A coin trading
$1M/day sends about $50/day to the $CHARLIE burn; $20M/day of enrolled volume
burns about $1,000/day. At 500 bps those halve, and at 50 bps a floor-sized
distribution yields less toll than the gas needed to move it. The dev keeps 90%
and decides how all of it splits.

**`TOLL_BPS` is also written into `charlie_pool` at initialisation, and
`distribute` asserts the constant and the stored value agree.** A constant
compiled into BPF is not on-chain state, and a protocol that asks strangers to
recompute its numbers cannot ask them to reproduce a build first. Stored, the
toll is one `getAccountInfo` away.

---

## 4. Accounts

```
route(mint)      = PDA(["route",   mint])   the dev's split, changeable
collector(mint)  = PDA(["collect", mint])   receives the whole creator fee
burn_pool(mint)  = PDA(["burn",    mint])   the coin's own buy-and-burn leg
charlie_pool     = PDA(["charlie"])         global; the toll, and TOLL_BPS
incinerator      = 1nc1nerator11111111111111111111111111111111
```

`route`:

```
sol_burn_bps   u16     to the incinerator
own_burn_bps   u16     to burn_pool(mint)
ops_bps        u16     to ops_address
ops_address    pubkey  an ordinary wallet
last_distrib   i64
bump           u8
```

Enforced on every write:

```
sol_burn_bps + own_burn_bps + ops_bps == 10000 - TOLL_BPS
```

There is no authority field. **`init_route` and `set_route` check the signer
against the coin's LIVE `sharing_config.admin`**, so the split follows the coin
rather than the wallet that happened to enrol it. That is safe precisely
because admin can no longer move: `transfer_fee_sharing_authority` is dead at
`6023`, and only pump's reset can install a new one. If pump ever does reset a
config, control of `route` follows the new admin, and the page should say so.

---

## 5. Instructions

```
init_route(mint, sol_burn_bps, own_burn_bps, ops_bps, ops_address)
    signer == sharing_config.admin
    refuse when bonding_curve.cashback is true          (section 6)
    refuse when the coin's quote mint is not wSOL
    creates route(mint) and collector(mint), rent exempt

set_route(mint, sol_burn_bps, own_burn_bps, ops_bps, ops_address)
    same signer check, same invariant, any time, as often as the dev likes

distribute(mint)                                        permissionless
    L = collector lamports - rent exempt minimum
    refuse unless L * TOLL_BPS / 10000 > k * reimbursement
    L * TOLL_BPS      / 10000 -> charlie_pool
    L * sol_burn_bps  / 10000 -> incinerator
    L * own_burn_bps  / 10000 -> burn_pool(mint)
    L * ops_bps       / 10000 -> route.ops_address
    the division remainder stays in the collector
    caller reimbursed min(actual fee, cap_bps * L / 10000), NO tip

crank_burn(mint)                                        permissionless
crank_charlie_burn()                                    permissionless
    atomic: pull -> swap -> SPL burn, one instruction
    lot = min(0.05 SOL, available), floor at rent exempt + fee
    per-coin cooldown, in-program bound, capped reimbursement
```

**Reimbursement pays the fee and never more.** A fixed tip is an extractable
bounty: a griefer with `ops_address` set to themselves calls `distribute` at
every interval boundary on their own coin and skims it, funded pro rata by the
toll and both burn legs. The crank's incentive is the ops share and the
protocol's own operation, not a bounty. And gating on `L` rather than on a
clock means a distribution that cannot pay for itself simply waits.

**The lot is `min(0.05 SOL, available)`.** A fixed 0.05 lot strands every
residue smaller than a lot, forever, in an immutable program. Most coins die
with less than 0.05 SOL in their own-burn pool.

`distribute` accepts **no addresses from its caller**. Every destination is
derived from the mint or read from `route`. That, plus the absence of any other
instruction that moves lamports out of `collector`, `burn_pool` or
`charlie_pool`, is the whole guarantee.

**Events.** `distribute` emits `DistributeEvent { mint, l, toll, sol_burn,
own_burn, ops, ops_address, toll_bps }` and each crank emits
`BurnEvent { mint, lamports_in, tokens_burned, supply_after }`. Without them a
stranger must attribute bare lamport deltas out of three PDAs with nothing
saying which leg a transfer belonged to, and "recomputable" stops being true.

---

## 6. Coins the protocol refuses

**Trader Cashback coins are refused outright.** Cashback is chosen at launch and
locked on chain, and it routes 100% of the creator fee to traders. Not just the
toll: the SOL burn, the dev's own buy-and-burn and their ops wallet all receive
nothing, because the creator vault never fills. Enrolling one is worse than
useless — it spends the coin's single irreversible update on a split that can
never pay out, and leaves the dev with `admin_revoked` set and no second chance.

The flag is three-valued and must be handled as three:

```
cashback is True    refuse, and say why
cashback is False   proceed
cashback is None    the curve predates the field. Proceed only past an
                    explicit confirmation: absent is not off, and the dev is
                    the one person who knows whether creator fees have ever
                    actually arrived in their wallet.
```

`/verify` stays open to cashback coins. They get a verdict explaining where
their fee goes; they simply cannot enrol.

**Non-wSOL quote mints are refused.** pump can whitelist other quote mints, and
such a coin pays creator fees as SPL tokens into a shareholder's ATA. The
collector has no instruction that moves SPL tokens, so those fees would be
stranded permanently. Assert the quote mint at `init_route` rather than discover
this after the freeze.

---

## 7. Enrolment, in one signature

```
create_fee_sharing_config     only when the coin has none, which is ~95% of
                              fresh launches. admin becomes the dev, initial
                              shareholders become [dev, 10000].
                              A GRADUATED coin must also pass pool,
                              pump_amm_program and pump_amm_event_authority,
                              or it fails outright.
init_route                    the dev's three shares; creates the collector
update_fee_shares_v2          collector(mint) at 10000 bps; spends the one-shot
```

All three go in **one transaction**, measured. The dev signs once.

Refuse before a wallet opens when: the coin is a cashback coin; the quote mint
is not wSOL; `admin_revoked` is true (which now means the one update is spent);
or the connected wallet is not the config's admin. The mainnet simulation gate
stays as the backstop that catches everything else in pump's own words.

**`update_fee_shares_v2` flushes the vault to the OUTGOING split before
replacing it** — it CPIs into `distribute_creator_fees_v2` — so its remaining
accounts are the current shareholders, and anything accrued pays out under the
old split. For a coin enrolling straight after creation that means the dev,
which is correct, and the page should say it plainly.

---

## 8. The crank, which is a pipeline and not an instruction

```
if pool exists and pool.coin_creator != sharing_config:
        pump_amm::migrate_pool_coin_creator          permissionless
if pool exists:
        pump_amm::transfer_creator_fees_to_pump      permissionless, no floor,
                                                     no-op when empty
pump::distribute_creator_fees                        pays collector(mint)
distribute(mint)                                     ours
```

Skipping the AMM transfer is the difference between paying zero and paying
everything on a graduated coin: six coins measured 0 lamports distributed alone
and their full balances after the transfer, one of them 101.4 SOL. `err = None`
in both cases, which is why the crank gates on `distributable_fees` and never
on the absence of an error.

Read the quote mint from the pool rather than assuming it. One enrolled pool in
59 sampled had a `coin_creator` of the zero pubkey — a pool older than the field
— and pays nothing until the permissionless migration is called.

---

## 9. What the indexer publishes

* **Enrolled** is two checks now: the config pays `collector(mint)` 10000 bps,
  and the coin is not a cashback coin.
* **Collected, not distributed** is its own row: lamports in `collector`, and
  for a graduated coin, wSOL still sitting in the AMM vault ATA. Neither is
  burned and neither is paid.
* **Below the reserve is pending, not failed.**
* **Four figures**, each recomputable from the program's own events: SOL burned
  at the incinerator, $CHARLIE burned, the coin's own supply burned, and ops
  paid.
* **`version` is recorded alongside `admin_revoked`.** A config whose `version`
  changed after enrolment is a coin pump reset out from under us, and the
  indexer should be able to name it rather than discover it in a figure.

---

## 10. Deploy order

1. Generate the program keypair; publish the id.
2. Write the program. `TOLL_BPS` in code and in `charlie_pool`.
3. Deploy **upgradeable**, and run the whole pipeline in production against one
   live coin, including a graduated one.
4. Only then revoke upgrade authority, and publish the reproducible build (the
   image digest, the build invocation, the expected ELF hash) so the freeze
   means something to a stranger.
5. Regenerate every surface so the pages describe what is deployed.

This resolves the contradiction between this document's earlier draft and
`ARCHITECTURE.md`. Freezing before anyone enrols would have shipped a permanent
bug on the graduated path, which was invisible until it was measured. Early
enrollers are enrolling into an upgradeable program, and the page says so in
those words.

---

## 11. What we may and may not claim

We may say the toll is unavoidable **by the dev**: they cannot reset, cannot
transfer authority, cannot revoke, and cannot update twice. All four measured.

We may not say "no questions asked" without qualification. pump's
`admin_set_creator_authority` can reset any config, re-arm the one-shot and
repoint the split, and pump can also rewrite `bonding_curve.creator`, toggle
cashback globally, divert a slice through buyback recipients, or deactivate fee
sharing entirely. None of that is reachable by a coin's owner, and all of it is
reachable by pump. The honest sentence is that the toll is unavoidable by the
dev and the protocol depends on pump the same way every pump coin does.

---

## 12. Still open

1. **Our program crediting the incinerator directly** has never been simulated,
   because the program does not exist. Devnet, before the freeze.
2. **The pump-can-reset result rests on one coin.** An RPC 429 ended the second.
3. **`6019` versus `3005`** on truncated optional AMM accounts is inferred.
4. **Everything is simulation against live state**, not landed transactions.
5. **The real shareholder cap is unknown.** `6011 TooManyShareholders` carries
   the message `"format"`, so the IDL never states the number; the SDK says 10
   and `indexer/enroll.py` says 8 with a comment claiming parity with
   `indexer/pump.py`'s 64, which is false. Under the collector shape we always
   write exactly one shareholder, so this only affects the reading path and the
   old multi-shareholder page.

# Charlie Protocol — build specification

What to build, with every number that comes from the chain rather than from a
document. Companion to `PROTOCOL.md` (the rules), `ARCHITECTURE.md` (the
system) and `FEE-ROUTING.md` (why the shape is what it is).

Everything in section 1 was measured against mainnet through crowd-api on
2026-09-03 and is reproducible: the `trace` workflow in the site repository
runs the same four tools on any push to `tools/`.

---

## 1. Measured, not assumed

| Fact | Value | How |
|---|---|---|
| Fee-share program | `pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ` | on-chain IDL |
| Split update instruction | `update_fee_shares_v2`, disc `6ffb31064e4e6a12` | on-chain IDL |
| Split is one-shot | `6024 FeeSharesAlreadyUpdated - Reward split can only be updated once` | on-chain IDL |
| Admin revocation is final | `6009 SharingConfigAdminRevoked` | on-chain IDL |
| Distribution instruction | `distribute_creator_fees`, disc `a572670079cef751`, **no signer** | on-chain IDL |
| Distribution v2 | `distribute_creator_fees_v2`, disc `ffcb134ff444089f`, signer `payer` | on-chain IDL |
| Remaining accounts | the config's shareholders, in its own order (`6054`) | on-chain IDL |
| Executable recipients refused | `6052` | on-chain IDL |
| Uninitialized recipients | `6070` exists but does NOT fire on the incinerator | simulation |
| Incinerator is payable | simulation of a live 100% config succeeds | simulation |
| Incinerator account | does not exist, and cannot | `getMultipleAccounts` |
| Distribution floor | `1781760` lamports of vault balance, below which the instruction returns success and distributes NOTHING | simulation log |
| Coins already routing to the incinerator | 419 configs | `getProgramAccounts` |
| Shareholder record | pubkey 32 + `share_bps` u16, bps must total 10000 | on-chain IDL |

**Do not hardcode the floor.** It was `1781760` today; read it per coin with
`get_minimum_distributable_fee` and treat a below-floor coin as pending, never
as failing.

---

## 2. What needs a program, and what does not

The SOL burn leg needs **no program of ours**. pump pays the incinerator
directly as a shareholder, and the runtime destroys the lamports at the end of
the block. That is live today and 419 coins use it.

The program exists for exactly three things:

1. **The $CHARLIE toll**, which is unavoidable only if it is taken somewhere
   the dev does not control.
2. **Buy-and-burn**, for the coin's own supply and for $CHARLIE, because SOL
   cannot destroy a token without a swap first.
3. **A split the dev can change**, since pump's is one-shot and permanent.

---

## 3. Accounts

```
route(mint)      = PDA(["route",   mint])   authority, three bps, ops_address
collector(mint)  = PDA(["collect", mint])   receives the whole creator fee
burn_pool(mint)  = PDA(["burn",    mint])   the coin's own buy-and-burn leg
charlie_pool     = PDA(["charlie"])         global, the toll accrues here
incinerator      = 1nc1nerator11111111111111111111111111111111
```

`route` fields:

```
authority     pubkey    the coin's pump sharing-config admin at init
sol_burn_bps  u16       to the incinerator
own_burn_bps  u16       to burn_pool(mint)
ops_bps       u16       to ops_address
ops_address   pubkey    an ordinary wallet
bump          u8
```

Invariant enforced on every write:

```
sol_burn_bps + own_burn_bps + ops_bps == 10000 - TOLL_BPS
```

`TOLL_BPS` is a `const` in the program. It is not a field, it is not an
argument, and there is no instruction that changes it.

---

## 4. Instructions

```
init_route(mint, sol_burn_bps, own_burn_bps, ops_bps, ops_address)
    signer must be the admin named by the coin's pump sharing config
    creates route(mint) and collector(mint), both rent exempt

set_route(mint, sol_burn_bps, own_burn_bps, ops_bps, ops_address)
    same signer check, same invariant
    this is the answer to pump's one-shot: our split stays changeable

distribute(mint)                                       permissionless
    L = collector lamports - rent exempt minimum
    refuse below a floor, and refuse before route.last_distributed + interval
    L * TOLL_BPS      / 10000 -> charlie_pool
    L * sol_burn_bps  / 10000 -> incinerator      burned by the runtime
    L * own_burn_bps  / 10000 -> burn_pool(mint)
    L * ops_bps       / 10000 -> route.ops_address
    remainder from integer division stays in the collector, never rounded away
    caller reimbursed actual fee plus a fixed tip, capped in bps

crank_burn(mint)                                       permissionless
crank_charlie_burn()                                   permissionless
    atomic: pull a fixed lot -> swap -> SPL burn, in ONE instruction
    0.05 SOL lots, in-program slippage bound, per-coin cooldown,
    capped reimbursement
```

`distribute` accepts **no addresses from its caller**. Every destination is
derived from the mint or read from `route`. That, plus the absence of any
instruction that moves lamports out of `collector`, `burn_pool` or
`charlie_pool` by any other path, is the entire guarantee.

**Upgrade authority MUST be revoked before any coin enrols.** Without it every
sentence above is a promise rather than a proof.

---

## 5. Enrolment

`/enroll` calls `update_fee_shares_v2` exactly as it does today, with one
shareholder:

```
collector(mint)   10000 bps
```

Preflight refuses, before a wallet opens, when: there is no sharing config,
`admin_revoked` is true, the connected wallet is not the admin, or the coin has
already spent its one update. The page states the one-shot rule before the
form, in pump's own words, because `6024` confirms it.

`collector(mint)` must exist and be rent exempt before the split points at it.
`init_route` creates it, so the order is: connect, `init_route`, then
`update_fee_shares_v2`.

---

## 6. What the indexer publishes

* **Enrolled** is one check: does the config pay `collector(mint)` 10000 bps.
* **A third state.** Lamports in `collector` are *collected, not distributed*.
  Not burned, not paid. Its own row, its own words.
* **Below the floor is pending, not failed.** A coin under
  `get_minimum_distributable_fee` cannot distribute yet, and saying so is the
  difference between a limit and a verdict.
* **Three separate figures**, each recomputable from the program's transfers:
  SOL burned at the incinerator, $CHARLIE burned by `crank_charlie_burn`, the
  coin's own supply burned by `crank_burn`.
* **`/verify` stays open to every coin**, enrolled or not. It is the growth
  surface, not a members' area.

---

## 7. Deploy order

1. Generate the program keypair. Publish the id.
2. Write the program. `TOLL_BPS` fixed in code.
3. Deploy, then **revoke upgrade authority**, then verify the deployed bytes
   against the source.
4. Regenerate the site so every page describes what is deployed.
5. Enrol $CHARLIE's own successor coin first, if there is one, before asking
   anyone else.

Nothing routes to a collector before step 3. An undeployed program means an
address that can receive and never send, and a dev's ops share would be
stranded there with everything else.

---

## 8. Decisions this spec is waiting on

1. **`TOLL_BPS`.** 50 (0.5% of the creator fee), 500 (5%), or 1000 (10%).
   Everything else is written; this is one constant and the copy that
   explains it.
2. **Migration.** Coins enrolled under the current multi-shareholder split
   cannot be converted, because `6024` is real: their one update is spent.
   Either stop enrolling into the current shape until the program ships, or
   accept two permanent classes of coin and say so on the page.
3. **Does `$CHARLIE` itself enrol?** Its config is `admin_revoked`, so it
   cannot. The protocol's own coin sits outside the protocol's own mechanism,
   and the site should say which of those two facts it leads with.

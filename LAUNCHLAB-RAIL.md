# The LaunchLab rail: stock-paired launches

> **DECIDED 20 September 2026: the rail ships as a platform, with no program
> of ours, and pays creators nothing.**
>
> Everything below section 1 was written for a different shape — a
> `charlie-launchlab` program owning every fee stream and routing it through
> four legs on chain. That program is not being built. It cost about 1.58 SOL
> fitted (3.3 as the runbook prices it) and its whole job was to route the
> creator's 0.50% to a PDA instead of a wallet.
>
> With the creator share at zero there is nothing of anyone else's to route.
> Charlie is the platform, the platform fee lands in a vault Charlie's keys
> own, and the burn legs run off chain from it exactly as they already do on
> the pump rail — claim, swap the quote to SOL when a route exists, buy
> $CHARLIE and burn it in one transaction. That is the shape StonkFun runs,
> and its on-chain cost is the platform config account: about 0.03 SOL.
>
> What this gives up is the same thing it gives up everywhere else: the legs
> are auditable rather than unredirectable (PROTOCOL.md sec.5). What it gives
> up specifically here is paying creators, which is the price of not
> custodying money owed to other people.
>
> Sections 2 onward are kept as the design that was costed and rejected, and
> as the measured Raydium facts behind it, which remain true.
>
> **Two things this decision requires that do not exist yet, so nothing here
> is shippable by following the runbook as written:**
>
> 1. **A platform-only setup path.** `tools/launchlab_mainnet_setup.py` deploys
>    the abandoned program and calls its `create_raydium_platform`, which takes
>    its rate from `CREATOR_FEE_RATE = 5_000` in `launchlab/src/lib.rs` — the
>    old 0.50%. Run today it would create a platform charging 1.00% in total and
>    route the creator share to program-owned accounts, contradicting the table
>    below. The platform config has to be created directly against Raydium with
>    a zero creator rate before the 0.50% figure describes anything real.
> 2. **A platform-vault keeper.** Nothing shipped claims a Raydium platform fee
>    and burns it. `launch-buyback.yml` spends the pump launch treasury against
>    its ledger; `indexer/launchlab_keeper.py` claims fees only through the
>    program that is not being deployed. The platform leg needs its own path:
>    claim, swap the quote to SOL when a route exists, then buy $CHARLIE and
>    burn it in one transaction. Until that exists the platform fee accrues in
>    its vault and burns nothing.
>
> So the fee table below is the **target shape of this decision, not the
> current behaviour of the tooling.**

Branch `claude/custom-pairs`. Written 18 September 2026. Phase 3 in the
order set in CUSTOM-PAIRS.md section 4: the pump SOL rail ships first, then
USDC, then this.

The pump rail enrolls coins that pump launched. This rail **launches** coins
itself, as a Raydium LaunchLab platform, the model StonkFun runs today. It
exists because pump pays a stock-paired coin's creator fee in the stock
token into accounts Charlie cannot make burn; on LaunchLab, Charlie is the
platform, the creator of every coin, and the owner of every fee stream, so
one program can claim each stream and route it through all four legs.

Every Raydium fact below was read from mainnet on 18 September 2026 (section
9 lists what and how). Numbers marked *estimate* are not measured yet; the
spike in section 8 measures them.

---

## 1. Who pays what

**On the bonding curve.** Fees are additive: a live StonkFun trade decoded
to protocol 0.250% + platform 1.000% = 1.250% of the quote amount.

| fee | rate | paid in | lands in | funds |
|---|---|---|---|---|
| Raydium protocol | 0.25% (`tradeFeeRate` 2500 on the SPYx, NVDAx and SOL configs) | quote | Raydium | -- |
| platform | **0.25%** | quote | Charlie platform fee vault | the Charlie leg |
| creator | **0%** (was 0.50%) | -- | -- | nothing: see the decision above |
| referral ("share") | 0% possible | -- | -- | `maxShareFeeRate` is 0 on stock configs |

Trader pays **0.50%**: Raydium's 0.25% and the platform's 0.25%. StonkFun
charges 1.25% and also pays creators nothing, so the comparison is a trader
paying 0.50% here against 1.25% there for the same thing.

On this rail the Charlie leg is exactly 0.25% of every curve trade, which is
the house line stated literally -- and with the creator share at zero it is
the only fee this project takes, so every lamport of it funds buying $CHARLIE
and burning it.

**What a dev gets for launching here, since it is no longer fee income:** a
quote asset pump cannot pair against without the fee arriving in a Token-2022
mint with a permanent delegate (CUSTOM-PAIRS.md sec.2), and a platform fee
that burns rather than accrues to an operator.

**After graduation** (Raydium CPMM, 0.25% tier, `cpswap_config` chosen at
platform creation):

| stream | share | owner | funds |
|---|---|---|---|
| LP fees, platform Fee Key NFT (one per pool) | `platform_scale` 1,000,000 | `ll_signer` | split by `crank_lp`: 25% the Charlie leg, 75% that coin's dev legs |
| LP, creator slice | `creator_scale` 0 | -- | LaunchLab refuses a creator slice (found on devnet, section 8) |
| LP burned | `burn_scale` 0 | -- | -- |
| CPMM creator fee, both tokens (`AmmCreatorFeeOn::BothToken`) | pool config's rate | `pool_creator` = `ll_collect(mint)` | the dev's legs |

`platformCpCreator` is **never set**: it is all-or-nothing across the
platform and would replace every coin's collector with one wallet.

## 2. Accounts

A new program, `charlie-launchlab`, separate from the pump collector in
`program/`. The pump program has been run on devnet and its audit surface
should not grow to carry Raydium CPIs. Same house style: native
`solana-program`, hand-rolled layouts, no Anchor.

| account | seeds | owner | holds |
|---|---|---|---|
| `ll_platform` | `["ll_platform"]` | program | Raydium platform config address, `charlie_mint`, admin multisig, paused flag, pending-change timelock |
| `ll_signer` | `["ll_signer"]` | **System, no data** | Raydium `platform_admin`, `platform_fee_wallet`, `platform_nft_wallet`. Holds platform Fee Key NFTs. |
| `ll_collect(mint)` | `["ll_collect", mint]` | **System, no data** | LaunchLab `creator` and CPMM `pool_creator`. Funded with rent at launch. |
| `ll_route(mint)` | `["ll_route", mint]` | program | `sol_burn_bps`, `own_burn_bps`, `ops_bps` (sum **10,000**), `ops_address`, coin admin, `quote_mint`, pool ids, lifetime totals |
| `quote_cfg(quote)` | `["quote_cfg", quote]` | program | enabled, oracle feed, `max_slippage_bps`, `max_staleness_s`, `min_crank` |

Why the collector and signer are **data-less System accounts**:
`claim_creator_fee` and `claim_platform_fee` make the claimant a writable
signer that pays rent for its recipient token account; a program-owned
account with data cannot pay. Mainnet shows the pattern working: PDA
`5jFLVBsJ...` (System-owned, no data) owns 10,627 Fee Key NFTs and claims
them through `CollectCpFee` invoked by program `BR336QJp...` (tx
`54zMKPpn...`); PDA `FWz75xYC...` signs `ClaimPlatformFee` from program
`CookM3w9...` (tx `3c6MAh2z...`).

Why route sums to 10,000 here and 7,500 on the pump rail: on pump the toll is
carved out of the creator fee inside the collector. Here the Charlie leg is
its own Raydium fee stream, so the coin's creator-side fees are all the
dev's.

Why `platform_admin` is a program PDA: Raydium's `update_platform_config`
can change the platform fee, the creator fee, both wallets and the LP scales
at any time. With `ll_signer` as admin, the only path to those changes is
this program's `propose_platform_change` / `apply_platform_change`, behind
the multisig and a published timelock.

## 3. Instructions

| # | instruction | signer | does |
|---|---|---|---|
| 1 | `init_platform` | admin | creates `ll_platform`; CPI `create_platform_config` with fee 2,500, creator fee 5,000, scales 250k/750k/0 |
| 2 | `set_quote` | admin | creates or updates `quote_cfg`; a quote is launchable only when enabled |
| 3 | `launch` | dev (payer) | checks `quote_cfg` and the quote-mint guards (section 5); CPI `initialize_with_token_2022` with `creator = ll_collect(mint)`, `amm_fee_on = BothToken`, **no** transfer-fee extension; creates `ll_route`; pre-creates `ll_collect`'s quote and coin token accounts; funds `ll_collect` with a rent buffer; optional dev buy |
| 4 | `set_route` | coin admin | new bps and `ops_address`; sum must be 10,000 |
| 5 | `crank_curve` | anyone | CPI `claim_creator_fee`, then `route_legs` |
| 6 | `crank_amm_creator` | anyone | CPI CPMM `collect_creator_fee_permissionless`, then `route_legs` |
| 7 | `crank_lp` | keeper | `ll_signer` claims the pool's platform Fee Key (`CollectCpFee`), 25% to the Charlie leg, 75% through `route_legs` |
| 8 | `crank_platform` | keeper | claims the platform fee (`claim_platform_fee` or `claim_platform_fee_from_vault`) and the platform Fee Key; swaps quote to SOL; pays the SOL into the pump rail's existing `charlie_pool`, which already buys and burns $CHARLIE |
| 9 | `propose_platform_change` / `apply_platform_change` | admin | the only route to Raydium `update_platform_config`, timelocked |
| 10 | `pause` | admin | stops cranks 5 to 8; never stops trading, which is Raydium's |

`route_legs(q, coin)` is shared by 5 to 7 and runs in the same transaction
as the claim, so nothing rests in a Charlie account between transactions:

1. **Guards** (section 5). Any failure aborts the whole transaction, and the
   fees stay unclaimed in Raydium's vaults.
2. **Own-token burn, coin side:** any coin the claim returned is burned
   (Token-2022 `burn`, `ll_collect` as owner). No swap.
3. **Own-token burn, quote side:** `own_burn_bps` of `q` buys the coin in its
   own pool (LaunchLab `buy_exact_in` before graduation, CPMM swap after),
   then burns it. No Jupiter.
4. **SOL burn:** `sol_burn_bps` of `q` goes through Jupiter to wSOL, output
   checked against the oracle bound; the wSOL account is closed with the
   **incinerator** as destination, so the lamports are destroyed.
5. **Ops:** `ops_bps` of `q` goes to `ops_address`'s token account by
   `transfer_checked`.
6. **Event:** one log line with source, `q`, coin burned, SOL burned, ops
   paid, route used.

A SOL-quoted coin runs the same instructions with the swap step skipped.

## 4. Fitting in one transaction

Measured with Jupiter's real swap instructions for a PDA swapper, compiled
as v0 with lookup tables:

| route | swap accounts (unique) | bytes with 40 of ours | price impact, ~$1k |
|---|---|---|---|
| SPYx multi-hop | 37 | 978 / 1,232 | 0.011% |
| SPYx direct (Raydium CLMM) | 23 | 797 / 1,232 | 0.231% |
| NVDAx multi-hop | 35 | 973 / 1,232 | 0.000% |
| NVDAx direct (Meteora DLMM) | 22 | 832 / 1,232 | 0.000% |

Bytes are never the limit. The 64 account locks per transaction are: a
multi-hop route plus ~40 of ours (*estimate*) is about 72. So:

- the keeper requests Jupiter routes with `maxAccounts` set to 64 minus the
  crank's own measured count (*estimate* ~20);
- one claim source per crank (5, 6 and 7 are separate) keeps our side
  small;
- one lookup table for the platform's static accounts, and one per coin
  created at graduation for its pool accounts;
- fallback if a route will not fit: skip the crank; fees wait in Raydium's
  vaults. Jito bundles are the escape hatch, not the design.

## 5. Guards

Checked on-chain in every crank, before any transfer:

- **Quote mint hook:** the Token-2022 `TransferHook` extension's program id
  must be `null`. Backed's hook authority (`5aMNNL...`) can set one; if it
  does, every xStock-quoted Raydium pool stops working, and the crank
  refuses rather than failing mid-route. Measured null on SPYx and NVDAx.
- **Pause:** the `Pausable` extension must read not paused.
- **Oracle bound on every Jupiter leg:** minimum SOL out = `q` x stock price
  x the mint's own `ScaledUiAmount` multiplier / SOL price x (1 - 
  `max_slippage_bps`). Prices from a pull oracle, rejected past
  `max_staleness_s`, which also stops swaps when the stock market is closed
  and its feed goes stale. This is what makes cranks 5 to 7 safe to leave
  permissionless: a caller can pick the route but cannot pick a bad price.
- **Minimum size:** `q >= min_crank`, so a keeper cannot grind dust through
  expensive routes.
- **Route read from `ll_route`, never from instruction data.** Destinations
  are derived or read from state, as in the pump program's `distribute`.

`crank_platform` is keeper-only because its last hop buys $CHARLIE, which has
no oracle. Its SOL goes into `charlie_pool`, and the existing buy-and-burn
path takes it from there with the same public events.

## 6. Off-chain

**Keeper.** Reads claimable balances (curve creator vaults, CPMM
`creator_fees_token_0/1`, lock position fees, platform vaults), cranks when
above `min_crank` and the oracle is fresh, builds v0 transactions with the
lookup tables, and caps Jupiter's `maxAccounts`. Watches every enabled quote
mint's hook program id and pause flag, and alerts on change.

**Indexer.** A `rail` column (`pump` | `launchlab`). Decodes the program's
events per coin. Reads LaunchLab `TradeEvent` for volume and fee totals.

**Site.** Per coin: the four legs with lifetime totals, the quote asset, and
an issuer panel for tokenized-stock quotes stating the permanent delegate,
freeze and pause powers, the hook authority, and that Raydium's programs are
upgradeable by one key (`FytDrVz...`).

## 7. Parameters

| parameter | value | changeable by |
|---|---|---|
| platform fee | 0.25% | admin via timelock |
| creator fee | 0.50% (Raydium's documented cap) | admin via timelock |
| LP scales | 100 / 0 / 0 (split 25 / 75 by `crank_lp`) | admin via timelock |
| `amm_fee_on` | `BothToken` | fixed per coin at launch |
| quote asset | allowlist | admin (`set_quote`); fixed per coin at launch |
| dev route | three legs summing to 10,000 | coin admin, any time |
| coin mint | Token-2022, no transfer fee | fixed |

## 8. Build order and tests

1. **Program skeleton and state**: accounts, `init_platform`, `set_quote`,
   `set_route`, unit tests for every layout and invariant, as the pump
   program has. **Built** in `launchlab/`: state, PDAs, instruction
   encoding, `init_platform` (upgrade authority only), `set_quote`,
   `set_route`, `set_paused`, `set_admin`, the quote-mint guards (tested
   against SPYx's live mint bytes) and the oracle floor. 27 tests,
   `cargo test --lib --tests`. Tags 10 to 16 parse and refuse until their
   step. The program id is a placeholder until the first devnet build.
2. **`launch` + `crank_curve`, SOL quote, devnet.** **Done, 18 September
   2026.** Program `4WgYTmPM9VHyFACSMdw4Bh9jkK9BETjWV5tNrDzuJWgX` on devnet,
   on-chain program data byte-identical to the build (SHA-256 `c3857a4d...`).
   - Raydium platform created by the program, `ll_signer` as admin, fee
     wallet and NFT wallet; fee 2,500, creator fee 5,000, scales
     1,000,000 / 0 / 0, `platformCpCreator` unset. Tx
     `3a9QcPgeXJ7z39s76DygbLiU5Yia863Qh2riAMwsfxNAokVw17q36Kg9EzLyFTRdJmtzk3zHwEVpyF2FasJbUYy7`.
   - `launch` with `ll_collect(mint)` as LaunchLab creator. Tx
     `4wnCdbDYfxFGtqGAcmawD7RDbzxBpR3X7btp3PzuRi2hwFYb7qdM7VGsn6VakFwN8VwjDZyGGXBWGq9xVapGs5hB`.
   - A 0.2 SOL trader buy paid 1,000,000 lamports to the coin's creator fee
     vault (0.50%) and 500,000 to the platform fee vault (0.25%).
   - `crank_curve`, 131,512 CU, tx
     `pkhWM6jq371aiNs8K4fMPoRovSHq8UUYVSFP3dAE7awvMyRrkuG7U3oWw47e8RmJPHSs6LgAKJaKD9RGyUpjukX`:
     claimed 1,000,000; own-burn leg bought 366,958.22 of the coin with
     400,000 and burned it (mint supply fell by exactly that); 400,000
     lamports to the incinerator; 200,000 to ops; remainder 0. Every lamport
     delta in the transaction reconciles.
   - Design change: `crank_curve` is keeper-only (the platform admin) until
     the own-burn buy has an on-chain price bound; a permissionless caller
     could otherwise sandwich a zero-minimum buy of the coin.
   - **Found on devnet:** LaunchLab refuses any creator LP slice
     (`creator_scale` must be 0). The LP now goes 100% to the platform
     Fee Key, one per pool, held by `ll_signer`; `crank_lp` (step 3) splits
     each pool's claim `LP_CHARLIE_BPS` (25%) to the Charlie leg and the
     rest through that coin's route. Same economics as section 1 intended.
   - **Open:** the ops and SOL-burn legs pay lamports by system transfer. A
     transfer that would leave a never-funded `ops_address` below the
     rent-exempt minimum fails and reverts the whole crank. Step 3 either
     requires a funded ops address at `set_route` or pays ops in the quote
     token.
3. **Graduation on devnet.** **Done, 18 September 2026.**
   - A coin launched through the program with a 0.3 SOL target filled and
     Raydium's devnet bot migrated it to CPMM. The pool's `pool_creator` is
     the coin's `ll_collect`; `ll_signer` received the pool's Fee Key NFT.
     Lock position layout (read from the devnet account): pool at 64, NFT
     mint at 96, LP mint at 160; `crank_lp` checks all three.
   - **Found on devnet:** the deployed CPMM is newer than the public
     `raydium-cp-swap` source (59fb845). `CollectCreatorFeePermissionless`
     takes two more accounts after the public 14: `amm_config`, then a
     read-only PDA `["creator_fee_share", creator, amm_config]`
     (`raydium-sdk-v2` 0.2.70-alpha). `AmmConfig` gained
     `creatorFeeShareRate` at offset 116. On the devnet config LaunchLab
     migrates into it is 0 and no share account exists for `ll_collect`;
     the crank below received 100% of the recorded creator fees. The SDK
     has no instruction that sets a share, so it is set on Raydium's side.
     **Open, mainnet:** read the mainnet config's rate and what a share does
     before the permanence copy is written; it is a second admin lever over
     routing, alongside pump's `admin_cto`.
   - `crank_amm_creator`, 112,944 CU, tx
     `5ypk6Rz79C21YRJq7Eay3vkXy3wUWNkVFDHXiYyhYkmX3qT8sKwFTrVjdpAn8GovBQb9zNKNEzL9s4cYLChbnWCJ`:
     the pool held 500,000 lamports and 547,257.01 coins of creator fees;
     all of both arrived. 200,000 bought coins, which were burned with the
     claimed coins (922,459.53 burned, mint supply fell by exactly that);
     200,000 to the incinerator; 100,000 to ops. 500 lamports of new creator
     fee (from the crank's own buy) stayed in the pool.
   - `crank_lp`, 267,715 CU, tx
     `2dJ8nDk1S9StqimdNpQUnSkwnqhsjCiMWZvthiCMP14PdNp5mMUQtv6iTRF4iQcT1a6Z9Sx17TZNQxrLQE4ubVq7`:
     198,540 lamports of LP fees; 49,635 (25%) to `charlie_pool`; of the
     148,905 routed, 59,562 to the incinerator, 59,562 bought and burned
     coins, 29,781 to ops. Coin-side LP fees burned with the bought coins
     (485,507.41).
   - **Keeper facts:** `lp_fee_amount` is clamped by the lock program to what
     is available, so the keeper passes `u64::MAX` (below about 1e5 the lock
     program rejects with 6006). The 33 accounts do not fit a legacy
     transaction with a compute-budget instruction (1,243 > 1,232 bytes) and
     the crank needs more than the default 200k CU, so it goes out as a v0
     transaction with an address lookup table (318 bytes; devnet table
     `6dGcRtawe3W1QMKuWBaP3BoWpNcbJNT5fHJLr8xrMovx`).
   - **Fixed after the first run:** the keeper paid for the wSOL accounts
     the post-graduation cranks open and close (about 3.0M lamports a run on
     devnet) and the rent went to `ll_signer`/`ll_collect`. `close_refund`
     now closes to the owner PDA and passes the rent to the keeper. Rerun on
     devnet: `crank_amm_creator` (tx
     `CcAeLt5XXRJsg1VpPUbrNathyrxaioCJMZ1mAiZEFp8FkixayoHxFpBwVFVcYjTaiX7FRrHav6ZXxp5gGvDZj9s`)
     left the keeper at +145,129 = ops 150,129 - fee 5,000; `crank_lp` (tx
     `3Js81pDkDQ9pgkzKiQKkQ9VEoHxyS6KuC8XQ664Utjahu1LUdkwzDGzM9nSerhrCyJuzts9mY7Lx5NpHZNZAmdp`)
     +25,988 = ops 30,988 - fee 5,000; `ll_signer` 0. Up to 2 lamports of
     rounding dust per crank stay with `ll_collect` (the legs floor).
   - **Mainnet read, 18 September 2026:** all 21 CPMM `AmmConfig` accounts
     have `creatorFeeShareRate` 0 and no `creator_fee_share` account exists.
     Nothing is diverted today; the lever is Raydium's, so the permanence
     copy names it.

4. **Account counts.** **Done.** `crank_curve` 26, `crank_amm_creator` 23,
   `crank_lp` 33, `crank_platform` 14 (pinned in
   `tests/test_launchlab_keeper.py`). Only `crank_lp` needs a lookup table;
   the other three fit legacy with a compute-budget instruction. Compute:
   131,512 / 131,943 / 258,683-267,715 / 67,316 CU. The Jupiter `maxAccounts`
   cap belongs to step 5 (stock-quote swaps) and is set there.
5. **Token-2022 quote.** **Not started, and blocked on two things this
   build cannot supply:** devnet has no xStocks and only Raydium can create
   a LaunchLab global config for a new quote, so the path needs either a
   local validator with LaunchLab, CPMM, the xStock mints and a Raydium
   global config cloned from mainnet, or Raydium adding a config. The legal
   gate on stock quotes (US persons) stands regardless.
6. **`crank_platform`.** **Done on devnet.** `ll_signer` claims LaunchLab's
   platform fee vault (`claim_platform_fee_from_vault`, SDK 0.2.70) and pays
   all of it to `charlie_pool`. Tx
   `2kbrzESNQ36kmzVAn8gEjLa9novDofBxuJSPi3qDonWUKhBTL9uLGK5BMLeWqGkr1g99YThEGp27NvggqFyuXUSd`:
   1,258,576 lamports, the whole vault; keeper paid only the fee; 67,316 CU.
7. **Keeper.** **Built:** `indexer/launchlab_keeper.py`, standard library
   only like the rest of the indexer. Finds every route, decides which of
   the four cranks has work, builds them, simulates each, sends only with
   `--send`. `crank_lp` goes out as v0 through a new
   `indexer.message.compile_v0`; its size matches the devnet transaction
   byte for byte (318). The `crank_platform` and `crank_amm_creator` account
   lists it derives are identical to the devnet transactions that succeeded.
   Checked end to end against live devnet: `plan()` and `build()` ran on
   devnet state captured 18 September 2026 (pinned as
   `tests/fixtures/launchlab_devnet_state.json`), and both transactions it
   built simulated successfully on devnet (`crank_amm_creator` 85,411 CU;
   `crank_lp` 215,062 CU, v0 through the lookup table). That run showed the
   planner crediting a 906-lamport creator fee that the program then claims
   but leaves unrouted (below `min_crank`); the planner now applies
   `min_crank` to the quote-side creator fee too. The build sandbox has no
   Solana RPC, so a keeper-signed send from Python is still to do:
   `python -m indexer.launchlab_keeper --cluster devnet --keypair
   <admin.json>` simulates; add `--send` to send.
   **Indexer and site panel: not built.** The route accounts carry the
   running totals (`coin_burned`, `lamports_burned`, `ops_paid`) and every
   crank logs `charlie:<crank>` data, so the panel reads those.
8. **Upgrade authority to the multisig.** **Not done; needs the multisig
   address.** Two steps once it exists: `set-upgrade-authority` for program
   data `BeG5M52m...` from the deployer to the multisig, then `set_admin` so
   the keeper key and the upgrade key are different people. On mainnet,
   deploy from the multisig from the start.

## 9. What was verified, and how

Read from mainnet through a public RPC on 18 September 2026:

- LaunchLab's on-chain IDL: `creator` is not a signer on
  `initialize_with_token_2022`; `claim_creator_fee` makes it a writable
  signer; `create_platform_config` is signed only by the platform's own
  admin; `create_platform_allow_config` is the platform's own opt-in. The
  quote token program is no longer fixed to legacy SPL.
- LaunchLab configs for SPYx, NVDAx and SOL exist with `tradeFeeRate` 2500;
  the stock configs have `maxShareFeeRate` 0.
- CPMM support-mint records exist for SPYx and NVDAx.
- SPYx and NVDAx mints: permanent delegate, pausable (not paused),
  freeze authority, default account state, confidential transfer mint,
  scaled UI amount, transfer hook with program `null`.
- StonkFun's two platform configs: fee 1%, creator fee 0, LP 100% to the
  platform, `platformCpCreator` set to its fee wallet.
- Three live StonkFun trades: 0.25% + 1.00% = 1.25%.
- 29 of 2,423 LaunchLab platforms use PDA wallets; the two transactions
  cited in section 2.
- LaunchLab, CPMM and the lock program share upgrade authority
  `FytDrVzDybM1TwFQPGb8qaxZR7dBCzNeqT3vtQsceZQK`.
- Jupiter swap instructions for SPYx and NVDAx to SOL, compiled with their
  lookup tables (section 4).

Source read: `raydium-io/raydium-cp-swap` at `59fb845` (the permissionless
creator-fee collect and the Token-2022 support list), `raydium-sdk-V2` at
`c289783` (fee arithmetic, lock claim layout), `raydium-idl` at `e7e0c96`.

Not verified: that a pull-oracle feed exists for each stock to be allowlisted
(`set_quote` refuses a quote without one), the creator-fee cap of 0.50% (Raydium's docs; confirm when
`create_platform_config` is first called on devnet), the CPMM creator-fee
rate on the chosen `cpswap_config`, and the lock program's source, which is
not public.

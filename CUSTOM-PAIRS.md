# Custom Pairs, USDC pairs, Holder Rewards, and pump's admin CTO

Branch `claude/custom-pairs`. Written 17 September 2026 against pump's
published IDLs at `pump-fun/pump-public-docs` `e0687ae` (the definitions this
code reads are extracted in `idl/pump-public-docs/`, see `SOURCE.md` there).

Between May and September pump added four things that change what a split
pays and who can change it. This branch makes the indexer read all four and
makes enrollment refuse, build, or hold each one on purpose. **No SOL-paired
enrollment byte changes**: `tests/test_quote_fields.py` pins the three SOL
messages to hashes generated from `main` at `f6e922e`.

> **PARKED 21 September 2026: USDC stays closed.** `NON_SOL_ENROLLMENT_OPEN`
> remains False and no USDC work is scheduled.
>
> Measured that day from pump's frontend API (`quote_mint` on each coin, a
> sample, not a full chain count). USDC was 0 of the 350 newest launches, 0 of
> the 300 most recently traded coins, 5 of the top 400 by market cap (about
> 0.3% of their combined market cap) and 1 of the top 200 graduated coins.
> Custom pairs outnumbered USDC in every sample. Four months after pump
> opened USDC pairs (21 May 2026), almost nothing launches or trades on them.
>
> Section 5 is superseded on one point: option (b) does **not** need the
> collector program. A USDC row can point at a Charlie-controlled wallet, and
> an off-chain keeper swaps the USDC to SOL, then burns it or hands it to
> `charlie-buyback`, the same trust position the $CHARLIE leg already holds
> (PROTOCOL.md sec.5). The cost is that this burn is keeper-dependent, unlike
> the SOL incinerator row, which pump pays directly.
>
> If USDC is reopened, the work is: a mainnet simulation of a USDC
> create-and-split; `distribute_creator_fees_v2` in `indexer/distribute.py`
> (it pays lamports through v1 today); the swap keeper; token inflows and
> swaps in the evidence record; a keeper-burn status in the checks; and page
> copy. `/launch` cannot make USDC coins at all: pump creates them only with
> `create_v2`, and the door uses the original `create`.

---

## 1. What the branch does

| change | where |
|---|---|
| reads `quote_mint`, `creator_fee_bps`, `is_holder_reward` off the bonding curve; `curve.quote` is `sol`, `usdc` or `custom` | `indexer/pump.py` |
| offsets recomputed from pump's IDL in a test, not typed from a comment | `tests/test_quote_fields.py` |
| `preflight` refuses Holder Rewards coins and Custom Pairs, with the reason addressed to the dev | `indexer/enroll.py` `refuse_quote` |
| USDC splits are **built** (quote mint, token program, shareholder ATAs in remaining accounts, USDC pool on the graduated create path) and **held**: `NON_SOL_ENROLLMENT_OPEN = False` | `indexer/enroll.py` |
| a USDC split with an incinerator row is refused even when open: USDC sent there is locked, not burned | `indexer/enroll.py` |
| the enroll API reports `quote`, `quote_mint`, `creator_fee_bps`, `holder_reward` on inspection and passes the quote through when building | `api/enroll.py` |
| `AdminCtoEvent` decoder and `find_admin_cto(tx, mints)` | `indexer/decode.py` |

## 2. Refused, and why

**Holder Rewards coins.** pump records a pump-controlled address as the
coin's creator and pays the creator fee out to holders. There is no creator
fee to split. pump's docs say a Holder Rewards coin never converts back.

**Custom Pairs** (tokenized stocks, wrapped majors, metals). The fee arrives
in the quote token, the runtime destroys lamports and not token balances, and
the quote assets themselves are the problem:

- xStocks mints are Token-2022 with a permanent delegate (the issuer can
  transfer or burn from any account, the collector's included), a pausable
  config, a transfer hook, and a scaled UI amount (dividends move a display
  multiplier, never the raw balance).
- Sunrise / Backpack Securities mints: extensions not yet read. Section 6.
- Jupiter routability for xStocks is intermittent, so a swap leg cannot
  assume a route at crank time.
- `creator_fee_bps` on a custom pair is changeable by pump's CTO team after
  launch, so the per-trade toll on these coins is not fixed by the dev or by
  us.

## 3. pump's admin can reset any enrolled split

The same IDL refresh added `pump::admin_cto`, signed by pump's
`admin_set_creator_authority`, taking `new_creator`, `is_holder_reward` and
`creator_fee_bps`, and emitting `AdminCtoEvent` with a `sharing_config_reset`
flag; and `pump_fees::admin_cto_sharing_config`, which sets a new admin on a
config. Neither exists in any earlier published IDL. On 14 September pump
changed its holder-rewards doc from "cannot be undone" to convertible
"through a community takeover".

This applies to **SOL-paired** coins as much as any other. So:

- "the split you set is the one you keep" and "pump's single irreversible
  config change" are true against the dev and not against pump. Copy needs
  to say which.
- The decoder is here; **wiring it into the scan and the verdict is not**
  (section 6). An enrolled mint that shows an `AdminCtoEvent` should lead its
  page with it.
- Caveat: this is read from the IDL and the event's field names, not the
  program source. The first real event is the check.

## 4. Phasing (decided 18 September 2026)

**Phase 1: SOL only.** Deploy the collector for SOL-paired coins and run a
coin through all four legs on mainnet (Charlie, SOL burn, own-token burn,
ops). Every non-SOL coin is refused at `preflight` exactly as this branch
does today; `NON_SOL_ENROLLMENT_OPEN` stays False for the whole phase.

**Phase 2: expand to USDC.** The collector grows a token path: own-token
burn first (it buys in the coin's own USDC pool, no swap), then the swap
legs, USDC to SOL for the incinerator and on to $CHARLIE. Custom Pairs stay
refused in both phases.

## 5. What must exist before USDC opens

`NON_SOL_ENROLLMENT_OPEN` stays False until every line here is done.

1. **A decision on the burn leg.** With the incinerator row refused, a USDC
   coin's split has no on-chain burn. Either (a) USDC coins enroll with no
   burn leg and the page says so, or (b) the collector program grows a token
   path: distribute USDC in, swap to SOL, send to the incinerator, **in one
   transaction** so nothing rests in a PDA. (b) is the only version that fits
   the thesis, and it is the phase 2 plan (section 4).
2. **If (b):** the collector program (`program/src/lib.rs`) moves lamports
   only today. It needs a token account per mint, `transfer_checked` CPIs,
   a swap CPI with a slippage cap and a no-route refusal, and events carrying
   amount in, SOL out and route. A devnet run through distribute, swap and
   burn, including the failure cases (no route, cap hit, missing ATA).
3. **The toll destination receives USDC** into its own ATA. The toll wallet
   is an ordinary on-curve wallet, so this works mechanically; how that USDC
   becomes $CHARLIE bought and burned is a public claim and needs writing
   down before the first one arrives.
4. **The toll line.** SOL and USDC coins use pump's standard schedule (USDC
   has its own `stable_fee_tiers`), so the per-trade figure is computable,
   but the USDC tiers are keyed to a USDC market cap. Check the figure
   against `get_fees_with_quote_mint` before it goes on a page.
5. **A mainnet simulation** of a USDC create-and-split, the same gate the SOL
   path passed on 2026-09-04. The builder's remaining-accounts shape follows
   pump's CREATOR_FEE_SHARING.md, which was written for USDC; it has not been
   simulated.

## 6. Not in this branch

- `AdminCtoEvent` wired into `scan` / `enrolled` / the coin page.
- Page copy: `/enroll` pairing refusal text, the permanence line, the reply
  protocol's "what about custom pairs" answer.
- Any change to `program/`.

## 7. Reads that need a Solana RPC

This branch was built without mainnet access. Before relying on it:

- **Sample the new curve fields**, as the cashback byte was: 200 coins pump
  itself labels (SOL, USDC, custom, holder rewards), decoded here, 100%
  agreement, output committed, then `pump.QUOTE_FIELDS_SAMPLED = True`.
- `Global.whitelisted_quote_mints` and pump's `QuoteControl` account: the
  live list of quote mints.
- `FeeConfig.exotic_flat_fees` in `pump_fees`: the protocol fee on custom
  pairs.
- Each Sunrise / Backpack mint's Token-2022 extensions.

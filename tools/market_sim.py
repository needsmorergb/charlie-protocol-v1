"""Simulated trading on $CHARLIE, driving the real splitter on devnet.

WHAT THIS IS. The splitter works -- `tools/splitter_devnet.py` proved the
three legs land in the right proportions. What that did not show is the
loop under TRADING: fees arriving continuously, at sizes a market actually
produces, with the creator fee moving as market cap moves. This does that.

It is a MOCK MARKET and a REAL SPLITTER. The distinction matters:

    simulated   the price path, the trade sizes, the volume
    real        pump's fee schedule, applied per trade at the right tier
    real        the constant-product pool arithmetic
    real        the splitter: a deployed program, landed transactions,
                a keyless vault, lamports actually destroyed

So nothing here is a claim about what $CHARLIE WILL do. It is a claim about
what the mechanism does when fees arrive, and the fee arithmetic in between
is pump's own rather than something invented to make the output look good.

THE STARTING STATE IS REAL. Read from $CHARLIE's live PumpSwap pool on
mainnet (2026-09-12): 301,891,848 $CHARLIE and 55.81 SOL in reserve, a
price of 0.000000184879 SOL and a market cap near 177 SOL. The simulated
market starts there, so the fee tier it lands on is the tier $CHARLIE
actually trades at.

WHY THE FEE FALLS AS THE PRICE RISES. pump's creator fee is a 25-tier
schedule keyed on MARKET CAP, and it falls as the cap climbs (BUILD.md
sec.3, read off pump's own FeeConfig accounts). A rally therefore pays a
SMALLER share of each trade, which is the opposite of the intuition and is
exactly the kind of thing a simulation is for. The tiers below are pump's,
not ours.

    python -m tools.market_sim --keypair PATH --scenario rally --trades 40

Each simulated trade accrues a creator fee. Once the accrued fee clears a
threshold the tool sends it to the splitter's vault and calls `split` -- a
real transaction, landing on devnet, dividing it 50/25/25. That is the
"pump pays the fee" step, and it is the one mocked link.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.flywheel_devnet import (
    RpcError,
    balance,
    balance_at_least,
    ix_transfer,
    load_keypair,
    minimum_balance,
    send,
    transaction_logs,
)
from tools.splitter_devnet import (
    CHARLIE_MINT,
    PROGRAM_ID,
    addresses,
    event_from_logs,
    ix_split,
)

LAMPORTS_PER_SOL = 1_000_000_000

# -- $CHARLIE's live pool, read from mainnet on 2026-09-12 ----------------
# The simulation starts from the real thing so the tier it trades at is the
# tier the coin is really on. These are raw units: 6 decimals for the token,
# lamports for SOL.
START_BASE_RESERVE = 301_891_848_592_000   # 301,891,848.592 CHARLIE
START_QUOTE_RESERVE = 55_813_500_000       # 55.8135 SOL
CHARLIE_SUPPLY = 956_381_633_039_163       # 956,381,633.039 CHARLIE
CHARLIE_DECIMALS = 6

# -- pump's graduated creator-fee schedule --------------------------------
# (market cap in SOL, creator fee in bps), highest cap first. BUILD.md
# sec.3; read off pump's FeeConfig account 5PHirr8joyTMp9JMm6nW7hNDVyEYdkzDqazxPD7RaTjx.
# The fee FALLS as the cap rises, which is pump's design and not ours.
CREATOR_FEE_TIERS = (
    (98_240, 5),
    (88_400, 10),
    (68_770, 20),
    (49_120, 30),
    (19_650, 60),
    (4_420, 75),
    (420, 95),
)
# Below the lowest graduated tier a coin still pays the bonding-curve rate.
CURVE_FEE_BPS = 30


def creator_fee_bps(market_cap_sol: float) -> int:
    """pump's creator fee for a coin at this market cap."""
    for floor, bps in CREATOR_FEE_TIERS:
        if market_cap_sol >= floor:
            return bps
    # A graduated coin below 420 SOL: the schedule's own bottom is 95 bps,
    # and that is where $CHARLIE sits today at ~177 SOL.
    return 95


# -- scenarios ------------------------------------------------------------
# Each is a drift and a volatility per trade, plus a buy bias. Nothing here
# is a forecast: they are shapes to exercise the mechanism under, and the
# tool prints which one it ran.
SCENARIOS = {
    "rally": {
        "buy_bias": 0.62,
        "size_sol": (0.05, 1.50),
        "why": "more buys than sells, trade sizes climbing -- the case where "
               "the creator fee per trade FALLS as the cap crosses tiers",
    },
    "chop": {
        "buy_bias": 0.50,
        "size_sol": (0.02, 0.40),
        "why": "balanced flow, small sizes -- the ordinary day, and the case "
               "that shows the split still clears at low volume",
    },
    "dump": {
        "buy_bias": 0.38,
        "size_sol": (0.05, 0.90),
        "why": "more sells than buys -- fees still accrue, because pump "
               "charges the creator fee on both sides of a trade",
    },
    "spike": {
        "buy_bias": 0.70,
        "size_sol": (0.20, 3.00),
        "why": "a burst of large buys -- the case where one split carries a "
               "much larger fee than the rest",
    },
}


class Market:
    """A constant-product pool, and pump's fee on each trade.

    `x * y = k` is the same invariant PumpSwap uses, so a trade's price
    impact here is the impact it would really have against this depth. The
    creator fee is taken off the quote side, which is where pump takes it.
    """

    def __init__(self, base: int = START_BASE_RESERVE, quote: int = START_QUOTE_RESERVE):
        self.base = base
        self.quote = quote
        self.volume_lamports = 0
        self.fees_lamports = 0
        self.trades = 0

    @property
    def price(self) -> float:
        """SOL per whole token."""
        return (self.quote / LAMPORTS_PER_SOL) / (self.base / 10**CHARLIE_DECIMALS)

    @property
    def market_cap_sol(self) -> float:
        return (CHARLIE_SUPPLY / 10**CHARLIE_DECIMALS) * self.price

    def buy(self, lamports_in: int) -> dict:
        """Spend `lamports_in` of SOL on tokens. The creator fee comes off
        the input before it reaches the pool, as pump does it."""
        bps = creator_fee_bps(self.market_cap_sol)
        fee = lamports_in * bps // 10_000
        net = lamports_in - fee
        # x*y=k: tokens out for `net` lamports in.
        tokens_out = self.base - (self.base * self.quote) // (self.quote + net)
        self.base -= tokens_out
        self.quote += net
        return self._record(lamports_in, fee, bps, "buy")

    def sell(self, lamports_target: int) -> dict:
        """Sell roughly `lamports_target` worth of tokens. pump charges the
        creator fee on sells too, which is why a dump still funds the legs."""
        bps = creator_fee_bps(self.market_cap_sol)
        # tokens needed to extract ~lamports_target, then the fee off the out.
        tokens_in = (self.base * lamports_target) // max(self.quote - lamports_target, 1)
        tokens_in = min(tokens_in, self.base // 10)  # never drain the pool
        gross = self.quote - (self.base * self.quote) // (self.base + tokens_in)
        fee = gross * bps // 10_000
        self.base += tokens_in
        self.quote -= gross
        return self._record(gross, fee, bps, "sell")

    def _record(self, gross: int, fee: int, bps: int, side: str) -> dict:
        self.volume_lamports += gross
        self.fees_lamports += fee
        self.trades += 1
        return {
            "side": side,
            "gross_lamports": gross,
            "creator_fee_lamports": fee,
            "creator_fee_bps": bps,
            "price": self.price,
            "market_cap_sol": self.market_cap_sol,
        }


def run(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keypair", required=True, type=Path)
    parser.add_argument("--ops", required=True,
                        help="the ops wallet the splitter's config names")
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), default="rally")
    parser.add_argument("--trades", type=int, default=40)
    parser.add_argument("--seed", type=int, default=7,
                        help="the RNG seed. Fixed by default so a run is reproducible.")
    parser.add_argument("--settle-every", type=int, default=8,
                        help="send accrued fees to the splitter every N trades")
    parser.add_argument("--scale", type=float, default=0.02,
                        help="fraction of each simulated fee actually sent on "
                             "devnet. The mechanism is proportional, and a "
                             "devnet wallet does not hold mainnet-sized fees.")
    parser.add_argument("--out", type=Path,
                        default=Path(__file__).resolve().parents[1] / "state" / "flywheel" / "market.json")
    args = parser.parse_args(argv)

    random.seed(args.seed)
    payer, seed = load_keypair(args.keypair)
    a = addresses(CHARLIE_MINT)
    scenario = SCENARIOS[args.scenario]

    market = Market()
    print(f"scenario  {args.scenario}: {scenario['why']}")
    print(f"program   {PROGRAM_ID} (devnet)")
    print(f"vault     {a['vault']}")
    print(f"start     price {market.price:.12f} SOL, "
          f"mcap {market.market_cap_sol:.1f} SOL, "
          f"fee {creator_fee_bps(market.market_cap_sol)} bps")
    print(f"scale     {args.scale:g} -- {args.scale:.0%} of each simulated fee is "
          f"actually sent on devnet\n")

    trades, settlements = [], []
    accrued = 0
    reserve = minimum_balance(0)

    for index in range(1, args.trades + 1):
        low, high = scenario["size_sol"]
        size = int(random.uniform(low, high) * LAMPORTS_PER_SOL)
        if random.random() < scenario["buy_bias"]:
            trade = market.buy(size)
        else:
            trade = market.sell(size)
        trade["trade"] = index
        trades.append(trade)
        accrued += trade["creator_fee_lamports"]

        if index % args.settle_every == 0 or index == args.trades:
            settlement = settle(payer, seed, a, args.ops, accrued, args.scale, reserve,
                                market, index)
            if settlement:
                settlements.append(settlement)
                accrued = 0

    # -- the summary ------------------------------------------------------
    simulated_fee = market.fees_lamports
    sent = sum(s["sent_lamports"] for s in settlements)
    legs = {leg: sum(s["event"][leg] for s in settlements)
            for leg in ("sol_burn", "buyback", "ops")}
    split_total = sum(s["event"]["l"] for s in settlements)

    print(f"\n  -- {market.trades} trades simulated --")
    print(f"  volume            {market.volume_lamports / LAMPORTS_PER_SOL:>12.4f} SOL")
    print(f"  creator fee       {simulated_fee / LAMPORTS_PER_SOL:>12.6f} SOL "
          f"({simulated_fee:,} lamports)")
    print(f"  price             {market.price:.12f} SOL "
          f"({market.price / (START_QUOTE_RESERVE / LAMPORTS_PER_SOL / (START_BASE_RESERVE / 10**CHARLIE_DECIMALS)) - 1:+.1%})")
    print(f"  market cap        {market.market_cap_sol:>12.1f} SOL")
    print(f"  fee tier now      {creator_fee_bps(market.market_cap_sol)} bps")
    print(f"\n  -- {len(settlements)} real splits on devnet --")
    print(f"  sent to vault     {sent:>12,} lamports")
    print(f"  actually split    {split_total:>12,} lamports")
    for leg, share in (("sol_burn", 50), ("buyback", 25), ("ops", 25)):
        got = legs[leg] / split_total * 100 if split_total else 0
        print(f"  {leg:15s}   {legs[leg]:>12,} lamports  {got:5.2f}%  (target {share}%)")

    record = {
        "cluster": "devnet",
        "program_id": PROGRAM_ID,
        "mint": CHARLIE_MINT,
        "scenario": args.scenario,
        "scenario_why": scenario["why"],
        "seed": args.seed,
        "scale": args.scale,
        "market_is_simulated": True,
        "splitter_is_real": True,
        "start": {
            "base_reserve": START_BASE_RESERVE,
            "quote_reserve": START_QUOTE_RESERVE,
            "price": START_QUOTE_RESERVE / LAMPORTS_PER_SOL / (START_BASE_RESERVE / 10**CHARLIE_DECIMALS),
            "source": "$CHARLIE's live PumpSwap pool, mainnet, 2026-09-12",
        },
        "end": {
            "base_reserve": market.base,
            "quote_reserve": market.quote,
            "price": market.price,
            "market_cap_sol": market.market_cap_sol,
            "creator_fee_bps": creator_fee_bps(market.market_cap_sol),
        },
        "simulated": {
            "trades": market.trades,
            "volume_lamports": market.volume_lamports,
            "creator_fee_lamports": simulated_fee,
        },
        "trades": trades,
        "settlements": settlements,
        "legs_real": legs,
        "split_total_lamports": split_total,
        "generated": int(time.time()),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {args.out}")
    return 0


def settle(payer, seed, a, ops, accrued, scale, reserve, market, index) -> dict | None:
    """Send the accrued fee to the vault and split it, for real.

    `scale` exists because a devnet wallet does not hold mainnet-sized fees
    and the mechanism is proportional: splitting 2% of each fee proves the
    same 50/25/25 that splitting all of it would. The record carries both
    the simulated figure and what was actually sent, so nothing is implied
    to be larger than it was.
    """
    sending = int(accrued * scale)
    if sending < 1_000:
        print(f"  [{index:>3}] accrued {accrued:,} -> scaled {sending:,}, "
              "below the floor; waiting")
        return None

    before_vault = balance(a["vault"])
    credit = send([ix_transfer(payer, a["vault"], sending)], payer, [seed])
    balance_at_least(a["vault"], before_vault + sending)
    signature = send([ix_split(CHARLIE_MINT, a, ops)], payer, [seed])
    event = event_from_logs(transaction_logs(signature))
    if event is None:
        raise RpcError("the split logged no event")

    print(f"  [{index:>3}] fee {accrued:>10,} -> sent {sending:>8,} -> "
          f"burn {event['sol_burn']:>8,} | buyback {event['buyback']:>8,} | "
          f"ops {event['ops']:>8,}   {signature[:8]}...")
    return {
        "after_trade": index,
        "simulated_fee_lamports": accrued,
        "sent_lamports": sending,
        "credit_signature": credit,
        "split_signature": signature,
        "event": event,
        "price_at_settlement": market.price,
        "market_cap_sol": market.market_cap_sol,
        "creator_fee_bps": creator_fee_bps(market.market_cap_sol),
    }


if __name__ == "__main__":
    raise SystemExit(run())

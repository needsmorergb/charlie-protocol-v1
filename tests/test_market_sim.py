"""The simulated market: that it uses pump's real fee schedule, and that the
mechanism it drives is proportional whatever the price does.

The risk with a simulation is that it flatters the thing it simulates. Two
guards against that here:

1. **The fee schedule is pump's, not ours.** Its tiers are asserted against
   the figures in `BUILD.md` section 3, which were read off pump's own
   `FeeConfig` accounts. A simulation that invented a friendlier fee curve
   would produce larger legs and prove nothing.
2. **Fees accrue on sells.** A simulation where only rallies fund the legs
   would be describing a mechanism that stops working the moment a coin
   goes down, which is not this one -- pump charges the creator fee on both
   sides.
"""

from __future__ import annotations

import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.market_sim import (  # noqa: E402
    CREATOR_FEE_TIERS,
    LAMPORTS_PER_SOL,
    SCENARIOS,
    Market,
    creator_fee_bps,
)


class FeeSchedule(unittest.TestCase):
    def test_the_tiers_are_pumps_own_figures(self):
        """BUILD.md section 3, read off pump's FeeConfig account."""
        self.assertEqual(
            dict(CREATOR_FEE_TIERS),
            {98_240: 5, 88_400: 10, 68_770: 20, 49_120: 30,
             19_650: 60, 4_420: 75, 420: 95},
        )

    def test_the_fee_falls_as_market_cap_rises(self):
        """pump's design, and the counter-intuitive half: a rally pays a
        SMALLER share of each trade."""
        caps = [500, 5_000, 20_000, 50_000, 70_000, 90_000, 100_000]
        fees = [creator_fee_bps(c) for c in caps]
        self.assertEqual(fees, sorted(fees, reverse=True))
        self.assertEqual(creator_fee_bps(500), 95)
        self.assertEqual(creator_fee_bps(100_000), 5)

    def test_charlie_sits_on_the_95_bps_tier(self):
        """~177 SOL market cap, read from the live pool. The bottom tier."""
        self.assertEqual(creator_fee_bps(176.8), 95)


class MarketArithmetic(unittest.TestCase):
    def test_it_starts_from_charlies_real_pool(self):
        market = Market()
        # 0.000000184879 SOL, the price implied by the live reserves.
        self.assertAlmostEqual(market.price, 1.84879e-7, places=12)
        self.assertAlmostEqual(market.market_cap_sol, 176.8, delta=0.5)

    def test_a_buy_raises_the_price_and_a_sell_lowers_it(self):
        market = Market()
        start = market.price
        market.buy(LAMPORTS_PER_SOL)
        self.assertGreater(market.price, start)
        high = market.price
        market.sell(LAMPORTS_PER_SOL // 2)
        self.assertLess(market.price, high)

    def test_sells_pay_the_creator_fee_too(self):
        """The mechanism does not stop working when a coin goes down."""
        market = Market()
        trade = market.sell(LAMPORTS_PER_SOL // 2)
        self.assertGreater(trade["creator_fee_lamports"], 0)
        self.assertEqual(trade["side"], "sell")

    def test_the_fee_is_the_tiers_share_of_the_trade(self):
        market = Market()
        trade = market.buy(LAMPORTS_PER_SOL)
        expected = LAMPORTS_PER_SOL * trade["creator_fee_bps"] // 10_000
        self.assertEqual(trade["creator_fee_lamports"], expected)

    def test_the_pool_is_never_drained_by_a_sell(self):
        market = Market()
        for _ in range(50):
            market.sell(LAMPORTS_PER_SOL * 10)
        self.assertGreater(market.base, 0)
        self.assertGreater(market.quote, 0)

    def test_every_scenario_produces_fees(self):
        """Including the one that goes down."""
        for name, scenario in SCENARIOS.items():
            with self.subTest(scenario=name):
                random.seed(7)
                market = Market()
                low, high = scenario["size_sol"]
                for _ in range(24):
                    size = int(random.uniform(low, high) * LAMPORTS_PER_SOL)
                    if random.random() < scenario["buy_bias"]:
                        market.buy(size)
                    else:
                        market.sell(size)
                self.assertGreater(market.fees_lamports, 0, name)
                self.assertEqual(market.trades, 24)

    def test_a_run_is_reproducible_from_its_seed(self):
        def once():
            random.seed(11)
            market = Market()
            for _ in range(15):
                market.buy(int(0.3 * LAMPORTS_PER_SOL))
            return market.price, market.fees_lamports

        self.assertEqual(once(), once())


if __name__ == "__main__":
    unittest.main()

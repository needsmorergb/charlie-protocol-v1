"""The launch-token and protocol-$CHARLIE routes cannot be confused."""

from __future__ import annotations

import unittest

from indexer import buyback_routes, legs


class TestBuybackRoutes(unittest.TestCase):
    def test_launch_route_refuses_charlie(self):
        with self.assertRaisesRegex(ValueError, "charlie-buyback"):
            buyback_routes.launch(buyback_routes.CHARLIE_MINT, "wallet")

    def test_launch_route_accepts_a_launch_mint(self):
        buyback_routes.launch("4dHdbxPANfuuvzXCTjngsqpjsUytE6KfBtQuGrT1nc1n", "wallet")

    def test_charlie_route_requires_collection_wallet(self):
        with self.assertRaisesRegex(ValueError, "collection wallet"):
            buyback_routes.charlie("wrong-wallet")
        buyback_routes.charlie(legs.TOLL_DESTINATION)

    def test_cli_exposes_distinct_routes(self):
        from indexer.cli import build_parser

        parser = build_parser()
        launch = parser.parse_args([
            "buyback", "4dHdbxPANfuuvzXCTjngsqpjsUytE6KfBtQuGrT1nc1n", "--wallet", legs.TOLL_DESTINATION,
        ])
        charlie = parser.parse_args(["charlie-buyback", "--wallet", legs.TOLL_DESTINATION])
        self.assertEqual(launch.command, "buyback")
        self.assertEqual(charlie.command, "charlie-buyback")

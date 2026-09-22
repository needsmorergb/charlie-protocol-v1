"""Offline tests for the X-tag payout ledger (`indexer.tag_ledger`)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from indexer import distribute, legs
from indexer import tag_launch as tag
from indexer import tag_ledger as ledger_mod
from indexer.base58 import encode
from indexer.tag_ledger import Ledger, claim_decision, inflow

TREASURY = legs.CHARLIE_PAYOUT_TREASURY
LAUNCHER = legs.CHARLIE_LAUNCH_WALLET
MINT = encode(bytes(range(1, 33)))
OTHER_MINT = "8FhAXv2tfXUpyMbJsHDHX9zfiEb9PERzFWSY9sgLpump"
CONFIG = "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"
DAY = ledger_mod.DAY
SOL = 1_000_000_000
T0 = 1_790_000_000


def distribute_ix(mint: str, *, data: str | None = None) -> dict:
    """A jsonParsed pump distribute instruction built from the crank's own
    account list: mint, bonding curve, config, creator vault, system,
    event authority, pump, then the shareholders."""
    shareholders = [legs.TOLL_DESTINATION, legs.SOL_BURN_INCINERATOR, legs.CHARLIE_OPS_DESTINATION, TREASURY]
    accounts = [a for a, _s, _w in distribute.accounts_for(mint, LAUNCHER, CONFIG, shareholders)]
    return {"programId": distribute.PUMP_PROGRAM, "accounts": accounts,
            "data": data or encode(distribute.DISTRIBUTE_CREATOR_FEES), "stackHeight": None}


def distribute_tx(mints=(MINT,), *, treasury_gain=4_000_000, err=None, graduated=False, data=None) -> dict:
    """The shape RpcClient.transaction returns (jsonParsed, legacy)."""
    instructions = [
        {"programId": "ComputeBudget111111111111111111111111111111", "accounts": [], "data": "3gJqkocMWaMm",
         "stackHeight": None},
    ]
    if graduated:
        instructions.append({"programId": distribute.PUMP_AMM_PROGRAM, "accounts": [LAUNCHER],
                             "data": encode(distribute.TRANSFER_CREATOR_FEES_TO_PUMP), "stackHeight": None})
    instructions += [distribute_ix(m, data=data) for m in mints]
    keys, seen = [LAUNCHER], {LAUNCHER}
    for ix in instructions:
        for account in [*ix["accounts"], ix["programId"]]:
            if account not in seen:
                seen.add(account)
                keys.append(account)
    pre = [5 * SOL] + [1_000_000] * (len(keys) - 1)
    post = list(pre)
    post[0] -= 5_000
    post[keys.index(TREASURY)] += treasury_gain
    return {
        "blockTime": T0,
        "slot": 400_000_000,
        "version": "legacy",
        "meta": {"err": err, "fee": 5_000, "preBalances": pre, "postBalances": post,
                 "innerInstructions": [], "logMessages": []},
        "transaction": {
            "signatures": ["sig"],
            "message": {
                "accountKeys": [{"pubkey": k, "signer": i == 0, "writable": True,
                                 "source": "transaction"} for i, k in enumerate(keys)],
                "instructions": instructions,
                "recentBlockhash": "11111111111111111111111111111111",
            },
        },
    }


class TestInflow(unittest.TestCase):
    def test_a_distribute_credits_its_mint_with_the_treasury_gain(self):
        self.assertEqual(inflow(distribute_tx(), TREASURY), [(MINT, 4_000_000)])

    def test_a_graduated_coin_s_two_instruction_tx_still_attributes(self):
        self.assertEqual(inflow(distribute_tx(graduated=True), TREASURY), [(MINT, 4_000_000)])

    def test_nothing_without_a_distribute(self):
        tx = distribute_tx()
        tx["transaction"]["message"]["instructions"] = tx["transaction"]["message"]["instructions"][:1]
        self.assertEqual(inflow(tx, TREASURY), [])

    def test_another_instruction_of_pump_is_not_a_distribute(self):
        self.assertEqual(inflow(distribute_tx(data=encode(bytes(8))), TREASURY), [])

    def test_failed_transactions_and_losses_count_for_nothing(self):
        self.assertEqual(inflow(distribute_tx(err={"InstructionError": [1, "x"]}), TREASURY), [])
        self.assertEqual(inflow(distribute_tx(treasury_gain=-10), TREASURY), [])
        self.assertEqual(inflow(None, TREASURY), [])

    def test_an_unknown_mint_is_nobody_s(self):
        self.assertEqual(inflow(distribute_tx(), TREASURY, known={OTHER_MINT}), [])

    def test_two_mints_in_one_tx_cannot_be_split(self):
        self.assertEqual(inflow(distribute_tx(mints=(MINT, OTHER_MINT)), TREASURY), [])

    def test_plain_string_account_keys_also_read(self):
        tx = distribute_tx()
        message = tx["transaction"]["message"]
        message["accountKeys"] = [k["pubkey"] for k in message["accountKeys"]]
        self.assertEqual(inflow(tx, TREASURY), [(MINT, 4_000_000)])


class ScanRpc:
    def __init__(self, txs: dict, order: list):
        self.txs, self.order = txs, order          # order: newest first

    def signatures_for_address(self, address, before=None, until=None, limit=1000):
        sigs = list(self.order)
        if until in sigs:
            sigs = sigs[:sigs.index(until)]
        if before in sigs:
            sigs = sigs[sigs.index(before) + 1:]
        return [{"signature": s, "err": None, "blockTime": T0} for s in sigs[:limit]]

    def transaction(self, signature):
        return self.txs.get(signature)


class TestLedger(unittest.TestCase):
    def setUp(self):
        self.book = tag.Book()
        self.ledger = Ledger(self.book.db)
        self.ledger.add_coin(MINT, "7", "alice", "100", T0)

    def test_it_shares_the_book_s_file(self):
        names = {r[0] for r in self.book.db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertTrue({"tag_requests", "tag_coins", "tag_credits", "tag_debits", "tag_wallets",
                         "tag_state"} <= names)

    def test_scan_credits_each_signature_and_mint_once(self):
        tx = distribute_tx()
        rpc = ScanRpc({"s1": tx, "s2": distribute_tx(mints=(OTHER_MINT,))}, ["s2", "s1"])
        got = self.ledger.scan(rpc, TREASURY)
        self.assertEqual([(r["signature"], r["user_id"], r["lamports"]) for r in got], [("s1", "7", 4_000_000)])
        self.assertEqual(self.ledger.state(ledger_mod.CURSOR_KEY), "s2")
        self.ledger.set_state(ledger_mod.CURSOR_KEY, None)     # a rescan from scratch
        self.assertEqual(self.ledger.scan(rpc, TREASURY), [])
        self.assertEqual(self.ledger.balance("7"), 4_000_000)
        self.assertFalse(self.ledger.credit("s1", MINT, 4_000_000, T0))

    def test_scan_stops_at_a_transaction_the_node_cannot_return_yet(self):
        rpc = ScanRpc({"s1": distribute_tx()}, ["s2", "s1"])
        self.ledger.scan(rpc, TREASURY)
        self.assertEqual(self.ledger.state(ledger_mod.CURSOR_KEY), "s1")
        rpc.txs["s2"] = distribute_tx(treasury_gain=1_000)
        self.ledger.scan(rpc, TREASURY)
        self.assertEqual(self.ledger.balance("7"), 4_001_000)

    def test_burnable_is_week_old_credit_not_covered_by_debits(self):
        self.ledger.credit("a", MINT, 3_000_000, T0)
        self.ledger.credit("b", MINT, 2_000_000, T0 + 5 * DAY)
        self.assertEqual(self.ledger.burnable("7", T0 + 6 * DAY), 0)
        self.assertEqual(self.ledger.burnable("7", T0 + 7 * DAY), 3_000_000)
        self.assertEqual(self.ledger.burns_at("7"), T0 + 7 * DAY)
        self.ledger.debit("7", "claim", 1_000_000, wallet="w", signature="c", at=T0 + DAY)
        self.assertEqual(self.ledger.burnable("7", T0 + 7 * DAY), 2_000_000)
        self.ledger.debit("7", "claim", 3_000_000, wallet="w", signature="d", at=T0 + DAY)
        self.assertEqual(self.ledger.burnable("7", T0 + 13 * DAY), 1_000_000)
        self.assertEqual(self.ledger.burns_at("7"), T0 + 12 * DAY)
        self.assertEqual(self.ledger.balance("7"), 1_000_000)
        with self.assertRaises(ValueError):
            self.ledger.debit("7", "gift", 1, at=T0)

    def test_mirror_writes_one_record_per_requester(self):
        self.ledger.credit("a", MINT, 3_000_000, T0)
        written = {}
        kv = mock.Mock(set_json=lambda key, value, ex=None: written.__setitem__(key, value))
        self.assertEqual(self.ledger.mirror(kv, T0 + 1), 1)
        self.assertEqual(written["claim:credit:7"], {
            "handle": "alice", "claimable": 3_000_000, "burns_at": T0 + 7 * DAY, "wallet": None,
            "wallet_pending": None, "wallet_ready_at": None, "updated": T0 + 1})

    def test_apply_wallet_binds_then_pends(self):
        first = claim_decision(**decide(bound=None))
        self.ledger.apply_wallet("7", first)
        self.assertEqual(self.ledger.wallet("7"), (WALLET, None, None))
        later = claim_decision(**decide(wallet=WALLET_2, bound=WALLET))
        self.ledger.apply_wallet("7", later)
        self.assertEqual(self.ledger.wallet("7"), (WALLET, WALLET_2, T0 + 72 * 3600))


WALLET = "So11111111111111111111111111111111111111112"
WALLET_2 = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"


def decide(**over) -> dict:
    base = dict(balance=2 * SOL, wallet=WALLET, bound=WALLET, pending=None, ready_at=None, now=T0,
                recipient_lamports=0, treasury_lamports=100 * SOL, paid_today=0)
    base.update(over)
    return base


class TestClaimDecision(unittest.TestCase):
    def test_pays_the_whole_balance_to_the_bound_wallet(self):
        self.assertEqual(claim_decision(**decide()), ledger_mod.Decision("pay", 2 * SOL))

    def test_the_first_wallet_binds(self):
        got = claim_decision(**decide(bound=None))
        self.assertEqual((got.state, got.bind), ("pay", WALLET))

    def test_a_different_wallet_waits_72_hours(self):
        got = claim_decision(**decide(wallet=WALLET_2))
        self.assertEqual((got.state, got.pending, got.ready_at), ("refused", WALLET_2, T0 + 72 * 3600))
        early = claim_decision(**decide(wallet=WALLET_2, pending=WALLET_2, ready_at=T0 + 72 * 3600,
                                        now=T0 + 71 * 3600))
        self.assertEqual((early.state, early.ready_at), ("refused", T0 + 72 * 3600))
        ready = claim_decision(**decide(wallet=WALLET_2, pending=WALLET_2, ready_at=T0 + 72 * 3600,
                                        now=T0 + 72 * 3600))
        self.assertEqual((ready.state, ready.bind), ("pay", WALLET_2))

    def test_a_third_wallet_restarts_the_wait(self):
        got = claim_decision(**decide(wallet="11111111111111111111111111111112", pending=WALLET_2,
                                      ready_at=T0 + 10, now=T0 + 20))
        self.assertEqual(got.ready_at, T0 + 20 + 72 * 3600)

    def test_below_the_minimum_is_refused(self):
        self.assertEqual(claim_decision(**decide(balance=999_999)).state, "refused")
        self.assertEqual(claim_decision(**decide(balance=1_000_000)).state, "pay")

    def test_a_new_recipient_must_end_rent_exempt(self):
        with mock.patch.object(ledger_mod, "MIN_CLAIM_LAMPORTS", 1):
            self.assertEqual(claim_decision(**decide(balance=890_879)).state, "refused")
            self.assertEqual(claim_decision(**decide(balance=890_880)).state, "pay")
            self.assertEqual(claim_decision(**decide(balance=10, recipient_lamports=SOL)).state, "pay")

    def test_the_treasury_keeps_its_reserve(self):
        got = claim_decision(**decide(treasury_lamports=2 * SOL + ledger_mod.TREASURY_RESERVE_LAMPORTS - 1))
        self.assertEqual(got.state, "held")
        self.assertEqual(claim_decision(**decide(
            treasury_lamports=2 * SOL + ledger_mod.TREASURY_RESERVE_LAMPORTS)).state, "pay")

    def test_caps_hold_the_claim_for_the_owner(self):
        self.assertEqual(claim_decision(**decide(balance=5 * SOL)).state, "pay")
        self.assertEqual(claim_decision(**decide(balance=5 * SOL + 1)).state, "held")
        self.assertEqual(claim_decision(**decide(paid_today=19 * SOL)).state, "held")
        self.assertEqual(claim_decision(**decide(balance=6 * SOL, caps=False)).state, "pay")

    def test_charlie_s_own_wallets_never_receive(self):
        for address in (legs.TOLL_DESTINATION, TREASURY, LAUNCHER):
            self.assertEqual(claim_decision(**decide(wallet=address, bound=None)).state, "refused")


if __name__ == "__main__":
    unittest.main()

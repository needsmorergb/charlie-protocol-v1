"""Offline tests for the X-tag payout ledger (`indexer.tag_ledger`)."""
from __future__ import annotations

import copy
import json
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
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "pump_distribute_mainnet.json"


def transfer(destination: str, lamports: int, height=2) -> dict:
    """A jsonParsed system transfer, as pump makes it for each shareholder row."""
    return {"program": "system", "programId": "11111111111111111111111111111111", "stackHeight": height,
            "parsed": {"type": "transfer", "info": {"source": "Vau1t11111111111111111111111111111111111111",
                                                     "destination": destination, "lamports": lamports}}}


def distribute_ix(mint: str, *, data: str | None = None) -> dict:
    """A jsonParsed pump distribute instruction built from the crank's own
    account list: mint, bonding curve, config, creator vault, system,
    event authority, pump, then the shareholders."""
    shareholders = [legs.TOLL_DESTINATION, legs.SOL_BURN_INCINERATOR, legs.CHARLIE_OPS_DESTINATION, TREASURY]
    accounts = [a for a, _s, _w in distribute.accounts_for(mint, LAUNCHER, CONFIG, shareholders)]
    return {"programId": distribute.PUMP_PROGRAM, "accounts": accounts,
            "data": data or encode(distribute.DISTRIBUTE_CREATOR_FEES), "stackHeight": None}


def distribute_tx(mints=(MINT,), *, treasury_gain=4_000_000, err=None, graduated=False, data=None,
                  inner=()) -> dict:
    """The shape RpcClient.transaction returns (jsonParsed, legacy)."""
    instructions = [
        {"programId": "ComputeBudget111111111111111111111111111111", "accounts": [], "data": "3gJqkocMWaMm",
         "stackHeight": None},
    ]
    if graduated:
        instructions.append({"programId": distribute.PUMP_AMM_PROGRAM, "accounts": [LAUNCHER],
                             "data": encode(distribute.TRANSFER_CREATOR_FEES_TO_PUMP), "stackHeight": None})
    share = max(treasury_gain, 0)
    inner_groups = []
    for m in mints:     # pump pays each row with its own nested system transfer
        instructions.append(distribute_ix(m, data=data))
        inner_groups.append({"index": len(instructions) - 1,
                             "instructions": [transfer(legs.TOLL_DESTINATION, 7), transfer(TREASURY, share)]})
    if inner:   # another program calling pump's distribute by CPI
        instructions.append({"programId": "Evi1Program11111111111111111111111111111111", "accounts": [LAUNCHER],
                             "data": "1", "stackHeight": None})
        nested = []
        for m in inner:
            nested += [dict(distribute_ix(m), stackHeight=2), transfer(TREASURY, share, height=3)]
        inner_groups.append({"index": len(instructions) - 1, "instructions": nested})
    keys, seen = [LAUNCHER], {LAUNCHER}
    for ix in instructions + [i for g in inner_groups for i in g["instructions"]]:
        for account in [*ix.get("accounts", []), ix["programId"]]:
            if account not in seen:
                seen.add(account)
                keys.append(account)
    pre = [5 * SOL] + [1_000_000] * (len(keys) - 1)
    post = list(pre)
    post[0] -= 5_000
    post[keys.index(TREASURY)] += treasury_gain * max(1, len(mints) + len(inner)) if treasury_gain > 0 \
        else treasury_gain
    return {
        "blockTime": T0,
        "slot": 400_000_000,
        "version": "legacy",
        "meta": {"err": err, "fee": 5_000, "preBalances": pre, "postBalances": post,
                 "innerInstructions": inner_groups, "logMessages": []},
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

    def test_two_mints_in_one_tx_each_get_their_own_transfers(self):
        self.assertEqual(inflow(distribute_tx(mints=(MINT, OTHER_MINT)), TREASURY),
                         [(MINT, 4_000_000), (OTHER_MINT, 4_000_000)])
        credits, rest, why = ledger_mod.classify(distribute_tx(mints=(MINT, OTHER_MINT)), TREASURY, {MINT})
        self.assertEqual((credits, rest, why), ([(MINT, 4_000_000)], 4_000_000, "unknown_mint"))

    def test_a_real_mainnet_distribute_pays_by_nested_system_transfers(self):
        # 2mA8Fp65..., one pump distribute; its shareholder rows are parsed
        # system transfers nested under it, and the one to the collection
        # wallet equals that wallet's balance change.
        tx = json.loads(FIXTURE.read_text(encoding="utf-8"))
        mint = tx["transaction"]["message"]["instructions"][0]["accounts"][0]
        self.assertEqual(ledger_mod.distributes(tx, legs.TOLL_DESTINATION), [(mint, 203_889)])
        self.assertEqual(ledger_mod.gain(tx, legs.TOLL_DESTINATION), 203_889)
        self.assertEqual(inflow(tx, legs.TOLL_DESTINATION, known={mint}), [(mint, 203_889)])

    def test_the_real_distribute_twice_in_one_tx_credits_each_exactly(self):
        tx = json.loads(FIXTURE.read_text(encoding="utf-8"))
        mint = tx["transaction"]["message"]["instructions"][0]["accounts"][0]
        second = copy.deepcopy(tx["transaction"]["message"]["instructions"][0])
        second["accounts"][0] = OTHER_MINT
        tx["transaction"]["message"]["instructions"].append(second)
        group = copy.deepcopy(tx["meta"]["innerInstructions"][0])
        group["index"] = 1
        for ix in group["instructions"]:
            if (ix.get("parsed") or {}).get("info", {}).get("destination") == legs.TOLL_DESTINATION:
                ix["parsed"]["info"]["lamports"] = 100_000
        tx["meta"]["innerInstructions"].append(group)
        at = [k["pubkey"] for k in tx["transaction"]["message"]["accountKeys"]].index(legs.TOLL_DESTINATION)
        tx["meta"]["postBalances"][at] += 100_000
        self.assertEqual(inflow(tx, legs.TOLL_DESTINATION, known={mint, OTHER_MINT}),
                         [(mint, 203_889), (OTHER_MINT, 100_000)])
        self.assertEqual(ledger_mod.classify(tx, legs.TOLL_DESTINATION, {OTHER_MINT}),
                         ([(OTHER_MINT, 100_000)], 203_889, "unknown_mint"))

    def test_nested_transfers_beyond_the_real_gain_credit_nothing(self):
        tx = distribute_tx()
        tx["meta"]["postBalances"][[k["pubkey"] for k in tx["transaction"]["message"]["accountKeys"]]
                                   .index(TREASURY)] -= 1
        self.assertEqual(ledger_mod.classify(tx, TREASURY, {MINT})[0], [])
        self.assertEqual(ledger_mod.classify(tx, TREASURY, {MINT})[2], "inconsistent")

    def test_a_transfer_beside_a_distribute_is_not_under_it(self):
        tx = distribute_tx(mints=(), inner=(MINT,))
        tx["meta"]["innerInstructions"][0]["instructions"].append(transfer(TREASURY, 9_000_000, height=2))
        tx["meta"]["postBalances"][[k["pubkey"] for k in tx["transaction"]["message"]["accountKeys"]]
                                   .index(TREASURY)] += 9_000_000
        self.assertEqual(ledger_mod.classify(tx, TREASURY, {MINT}),
                         ([(MINT, 4_000_000)], 9_000_000, "not_distributed"))

    def test_an_inner_distribute_of_a_known_mint_is_credited(self):
        tx = distribute_tx(mints=(), inner=(MINT,))
        self.assertEqual(inflow(tx, TREASURY, known={MINT}), [(MINT, 4_000_000)])

    def test_a_cpi_distribute_cannot_ride_on_a_known_one(self):
        # A real tag coin's distribute, plus another coin's by CPI: the tag
        # coin gets exactly its own transfer, the other coin's is nobody's.
        tx = distribute_tx(mints=(MINT,), inner=(OTHER_MINT,))
        self.assertEqual(inflow(tx, TREASURY, known={MINT}), [(MINT, 4_000_000)])
        self.assertEqual(ledger_mod.classify(tx, TREASURY, {MINT})[1:], (4_000_000, "unknown_mint"))

    def test_an_inner_distribute_without_nesting_heights_is_not_guessed(self):
        tx = distribute_tx(mints=(), inner=(MINT,))
        for ix in tx["meta"]["innerInstructions"][0]["instructions"]:
            ix["stackHeight"] = None
        self.assertEqual(ledger_mod.classify(tx, TREASURY, {MINT}), ([], 4_000_000, "unreadable_nesting"))

    def test_an_inner_only_distribute_of_an_unknown_mint_is_unattributed(self):
        tx = distribute_tx(mints=(), inner=(OTHER_MINT,))
        self.assertEqual(inflow(tx, TREASURY, known={MINT}), [])
        self.assertEqual(ledger_mod.classify(tx, TREASURY, {MINT})[2], "unknown_mint")

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
        got = self.ledger.scan(rpc, TREASURY, now=T0 + 99)
        self.assertEqual([(r["kind"], r["signature"], r.get("user_id"), r["lamports"]) for r in got],
                         [("credit", "s1", "7", 4_000_000), ("unattributed", "s2", None, 4_000_000)])
        self.assertEqual(got[0]["at"], T0 + 99)          # dated when recorded, not the block time
        self.assertEqual(self.ledger.state(ledger_mod.CURSOR_KEY), "s2")
        self.ledger.set_state(ledger_mod.CURSOR_KEY, None)     # a rescan from scratch
        self.assertEqual([r["kind"] for r in self.ledger.scan(rpc, TREASURY, now=T0)], ["unattributed"])
        self.assertEqual(self.ledger.balance("7"), 4_000_000)
        self.assertFalse(self.ledger.credit("s1", MINT, 4_000_000, T0))

    def test_scan_stops_at_a_transaction_the_node_cannot_return_yet(self):
        rpc = ScanRpc({"s1": distribute_tx()}, ["s2", "s1"])
        self.ledger.scan(rpc, TREASURY, now=T0)
        self.assertIsNone(self.ledger.state(ledger_mod.CURSOR_KEY))     # not moved: s2 is unread
        rpc.txs["s2"] = distribute_tx(treasury_gain=1_000)
        self.ledger.scan(rpc, TREASURY, now=T0)
        self.assertEqual(self.ledger.balance("7"), 4_001_000)
        self.assertEqual(self.ledger.state(ledger_mod.CURSOR_KEY), "s2")

    def test_scan_walks_every_page_back_to_the_cursor(self):
        order = [f"s{i}" for i in range(60, 0, -1)]
        rpc = ScanRpc({s: distribute_tx(treasury_gain=10) for s in order}, order)
        with mock.patch.object(ledger_mod, "SCAN_PAGE", 2):
            got = self.ledger.scan(rpc, TREASURY, now=T0)
        self.assertEqual(len(got), 60)
        self.assertEqual(got[0]["signature"], "s1")        # oldest first
        self.assertEqual(self.ledger.state(ledger_mod.CURSOR_KEY), "s60")

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

    def test_pending_debits_lower_the_balance_until_dropped_or_final(self):
        self.ledger.credit("a", MINT, 3_000_000, T0)
        self.ledger.debit_pending("claim", [("7", 3_000_000)], wallet=WALLET, signature="p1", at=T0,
                                  last_valid=500)
        self.assertEqual(self.ledger.balance("7"), 0)
        self.assertTrue(self.ledger.has_pending("7"))
        self.assertEqual(self.ledger.pending()["p1"]["rows"], [("7", 3_000_000, WALLET)])
        self.assertEqual(self.ledger.pending()["p1"]["last_valid"], 500)
        self.ledger.drop_pending("p1")
        self.assertEqual(self.ledger.balance("7"), 3_000_000)
        self.ledger.debit_pending("claim", [("7", 3_000_000)], wallet=WALLET, signature="p2", at=T0,
                                  last_valid=500)
        self.ledger.finalize("p2")
        self.ledger.drop_pending("p2")                     # a final debit is never dropped
        self.assertEqual((self.ledger.balance("7"), self.ledger.pending()), (0, {}))

    def test_held_and_approved_claims_do_not_burn(self):
        self.ledger.credit("a", MINT, 30_000_000, T0)
        self.ledger.hold("7", 30_000_000, WALLET, T0)
        self.ledger.hold("7", 30_000_000, WALLET, T0 + 1)          # updated, not doubled
        self.assertEqual(self.ledger.burnable("7", T0 + 8 * DAY), 0)
        self.assertIsNone(self.ledger.burns_at("7"))
        self.assertEqual(self.ledger.approve_held("7"), 1)
        self.assertEqual(self.ledger.approved(), [("7", WALLET, 30_000_000)])
        self.assertEqual(self.ledger.burnable("7", T0 + 8 * DAY), 0)
        self.ledger.hold("7", 90_000_000, WALLET, T0 + 2)          # an approval never grows
        self.assertEqual(self.ledger.approved(), [("7", WALLET, 30_000_000)])
        self.ledger.drop_held("7")
        self.assertEqual(self.ledger.approved(), [])
        self.assertEqual(self.ledger.burnable("7", T0 + 8 * DAY), 30_000_000)

    def test_a_hold_is_paid_only_when_its_payment_is_final(self):
        self.ledger.credit("a", MINT, 30_000_000, T0)
        self.ledger.hold("7", 30_000_000, WALLET, T0)
        self.ledger.approve_held("7")
        self.ledger.debit_pending("claim", [("7", 30_000_000)], wallet=WALLET, signature="p1", at=T0,
                                  last_valid=500)
        self.ledger.link_held("7", "p1")
        self.assertEqual(self.ledger.approved(), [])                     # being paid
        self.assertEqual(self.ledger.burnable("7", T0 + 8 * DAY), 0)
        self.ledger.drop_pending("p1")                                   # failed: approved again
        self.assertEqual(self.ledger.approved(), [("7", WALLET, 30_000_000)])
        self.assertEqual(self.ledger.burnable("7", T0 + 8 * DAY), 0)
        self.ledger.debit_pending("claim", [("7", 30_000_000)], wallet=WALLET, signature="p2", at=T0,
                                  last_valid=500)
        self.ledger.link_held("7", "p2")
        self.ledger.finalize("p2")
        self.assertEqual(self.ledger.db.execute("SELECT state FROM tag_held").fetchall(), [("paid",)])
        self.assertEqual((self.ledger.approved(), self.ledger.held_lamports("7")), ([], 0))

    def test_the_hold_is_linked_in_the_same_write_as_its_debit(self):
        self.ledger.credit("a", MINT, 30_000_000, T0)
        self.ledger.hold("7", 30_000_000, WALLET, T0)
        self.ledger.approve_held("7")
        self.ledger.debit_pending("claim", [("7", 30_000_000)], wallet=WALLET, signature="p1", at=T0,
                                  last_valid=500, held="7")
        self.assertEqual(self.ledger.approved(), [])
        self.assertEqual(self.ledger.db.execute("SELECT signature FROM tag_held").fetchall(), [("p1",)])

    def test_a_signature_already_on_the_books_is_refused(self):
        self.ledger.credit("a", MINT, 60_000_000, T0)
        self.ledger.debit_pending("claim", [("7", 3_000_000)], wallet=WALLET, signature="p1", at=T0,
                                  last_valid=500)
        with self.assertRaises(ValueError):
            self.ledger.debit_pending("claim", [("8", 3_000_000)], wallet=WALLET, signature="p1", at=T0,
                                      last_valid=500)
        self.assertEqual(self.ledger.pending()["p1"]["rows"], [("7", 3_000_000, WALLET)])

    def test_an_old_database_gains_the_new_columns(self):
        import sqlite3
        db = sqlite3.connect(":memory:")
        db.executescript("CREATE TABLE tag_debits (id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, kind TEXT NOT NULL, "
                         "lamports INTEGER NOT NULL, wallet TEXT, signature TEXT, at INTEGER NOT NULL, "
                         "state TEXT NOT NULL DEFAULT 'final');"
                         "CREATE TABLE tag_held (id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, "
                         "lamports INTEGER NOT NULL, wallet TEXT NOT NULL, at INTEGER NOT NULL, state TEXT NOT NULL);")
        ledger = Ledger(db)
        ledger.debit_pending("burn", [("7", 1)], wallet=WALLET, signature="p", at=T0, last_valid=9)
        self.assertEqual(ledger.pending()["p"]["last_valid"], 9)
        ledger.hold("7", 5, WALLET, T0)
        self.assertEqual(ledger.held_lamports("7"), 5)

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
        self.assertEqual(claim_decision(**decide()), ledger_mod.Decision("pay", 2 * SOL, bind=WALLET))

    def test_a_claim_to_the_bound_wallet_clears_a_pending_change(self):
        got = claim_decision(**decide(pending=WALLET_2, ready_at=T0 + 100))
        self.assertEqual((got.state, got.bind), ("pay", WALLET))

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
            # the fee comes out of the amount, so the wallet must end rent-exempt after it
            self.assertEqual(claim_decision(**decide(balance=895_879)).state, "refused")
            self.assertEqual(claim_decision(**decide(balance=895_880)).state, "pay")
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

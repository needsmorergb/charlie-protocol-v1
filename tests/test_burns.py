"""Offline tests for the burn decoders, `initial_supply` derivation, the
mint-wide burn scan, and `BURN_SUPPLY` on real evidence.

`python -m unittest discover -s tests -t tests -p "test_burns.py"`.

No network. Every fixture fed to a decoder is built byte by byte here, in the
style of `tests/test_indexer.py`'s `mint_account()`/`config_account()` -- a
pump layout change must show up as a failing decode test, never as a wrong
number in a published post. None of these fixtures is a captured RPC response
pasted in whole.
"""

from __future__ import annotations

import base64
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from indexer import decode
from indexer.base58 import pubkey_bytes
from indexer.evidence import Evidence
from indexer.pump import DecodeError, TOKEN_2022_PROGRAM, TOKEN_PROGRAM
from indexer.scan import derive_initial_supply

CHARLIE = "8FhAXv2tfXUpyMbJsHDHX9zfiEb9PERzFWSY9sgLpump"
CHARLIE_CURVE = "7VxCTsEknMC9ofXsddPM8piaGorGrMR8FQnDFjsQ7bjx"
OTHER_MINT = "So11111111111111111111111111111111111111112"
BONDING_CURVE = "7VxCTsEknMC9ofXsddPM8piaGorGrMR8FQnDFjsQ7bjx"
POOL = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
AUTHORITY = "2CFywHXDPjDK2iRQsb95vnjgncDUZeQKJ6MceJ4ALpdc"
USER = "So11111111111111111111111111111111111111112"
CREATOR = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

# RESEARCH.md Q8's pinned $CHARLIE boost totals -- a regression vector.
CHARLIE_BOOST_TOKENS_BURNED = 43_575_480_427_900  # 43,575,480.427900 tokens: boost-caused
                                                    # supply destruction, NOT protocol-attributed (D-11)
CHARLIE_INITIAL_SUPPLY = 1_000_000_000_000_000  # 1,000,000,000.000000 tokens at 6 decimals


# -- fixtures ---------------------------------------------------------------
def burn_instruction(mint, amount, program_id=TOKEN_2022_PROGRAM, account="acct-1",
                      authority="auth-1", checked=False):
    parsed = {
        "type": "burnChecked" if checked else "burn",
        "info": {
            "account": account,
            "amount": str(amount),
            "authority": authority,
            "mint": mint,
        },
    }
    if checked:
        parsed["info"]["decimals"] = 6
    return {"parsed": parsed, "program": "spl-token", "programId": program_id, "stackHeight": 1}


def non_burn_instruction(program_id=TOKEN_PROGRAM):
    return {
        "parsed": {"type": "transfer", "info": {"destination": "x", "amount": "1"}},
        "program": "spl-token",
        "programId": program_id,
    }


def tx_with(top_instructions=None, inner=None, log_messages=None, err=None):
    return {
        "transaction": {"message": {"instructions": top_instructions or []}},
        "meta": {
            "err": err,
            "innerInstructions": inner or [],
            "logMessages": log_messages or [],
        },
        "blockTime": 1_000,
        "slot": 10,
    }


def program_data_log(payload: bytes) -> str:
    return "Program data: " + base64.b64encode(bytes(payload)).decode()


def boost_event_payload(
    mint=CHARLIE, bonding_curve=BONDING_CURVE, pool=POOL, authority=AUTHORITY,
    quote_requested=1_000_000, quote_used=900_000, base_burned=1_500_000_000,
    timestamp=1_700_000_000,
) -> bytearray:
    payload = bytearray()
    payload += decode.DISC_BOOST_BUY_AND_BURN
    payload += timestamp.to_bytes(8, "little", signed=True)
    payload += pubkey_bytes(mint)
    payload += pubkey_bytes(bonding_curve)
    payload += pubkey_bytes(pool)
    payload += pubkey_bytes(authority)
    payload += quote_requested.to_bytes(8, "little")
    payload += quote_used.to_bytes(8, "little")
    payload += base_burned.to_bytes(8, "little")
    return payload


def buy_event_payload() -> bytearray:
    """A `BuyEvent` payload -- emitted immediately before the boost event in
    every real crank. Its exact field layout doesn't matter here; only its
    discriminator does, since `decode_boost_event` must reject it on that
    basis alone.
    """
    payload = bytearray()
    payload += decode.DISC_BUY_EVENT
    payload += bytes(64)  # arbitrary body -- never read
    return payload


def create_event_payload(
    name="charlie", symbol="CHARLIE", uri="https://example.invalid/charlie.json",
    mint=CHARLIE, bonding_curve=BONDING_CURVE, user=USER, creator=CREATOR,
    timestamp=1_600_000_000, virtual_token_reserves=0, virtual_sol_reserves=0,
    real_token_reserves=0, token_total_supply=CHARLIE_INITIAL_SUPPLY,
) -> bytearray:
    def write_str(value: str) -> bytes:
        raw = value.encode("utf-8")
        return len(raw).to_bytes(4, "little") + raw

    payload = bytearray()
    payload += decode.DISC_CREATE_EVENT
    payload += write_str(name)
    payload += write_str(symbol)
    payload += write_str(uri)
    payload += pubkey_bytes(mint)
    payload += pubkey_bytes(bonding_curve)
    payload += pubkey_bytes(user)
    payload += pubkey_bytes(creator)
    payload += timestamp.to_bytes(8, "little", signed=True)
    payload += virtual_token_reserves.to_bytes(8, "little")
    payload += virtual_sol_reserves.to_bytes(8, "little")
    payload += real_token_reserves.to_bytes(8, "little")
    payload += token_total_supply.to_bytes(8, "little")
    return payload


# -- anchor_discriminator ----------------------------------------------------
class TestAnchorDiscriminator(unittest.TestCase):
    def test_pinned_constants_match_the_helper(self):
        self.assertEqual(decode.anchor_discriminator("BoostBuyAndBurnEvent"), decode.DISC_BOOST_BUY_AND_BURN)
        self.assertEqual(decode.anchor_discriminator("CreateEvent"), decode.DISC_CREATE_EVENT)
        self.assertEqual(decode.anchor_discriminator("BuyEvent"), decode.DISC_BUY_EVENT)


# -- find_burns ---------------------------------------------------------------
class TestFindBurns(unittest.TestCase):
    def test_token_2022_burn_is_found(self):
        tx = tx_with(top_instructions=[burn_instruction(CHARLIE, 100, program_id=TOKEN_2022_PROGRAM)])
        burns = decode.find_burns(tx, CHARLIE)
        self.assertEqual(len(burns), 1)
        self.assertEqual(burns[0]["amount"], 100)

    def test_classic_token_burn_is_found(self):
        tx = tx_with(top_instructions=[burn_instruction(CHARLIE, 50, program_id=TOKEN_PROGRAM)])
        burns = decode.find_burns(tx, CHARLIE)
        self.assertEqual(len(burns), 1)

    def test_unrelated_program_id_is_not_found(self):
        """RESEARCH.md Pitfall 1: the friendly 'program' label is identical
        for Token and Token-2022 -- filtering on programId is what actually
        excludes a non-token program even though `program`/`parsed.type`
        might otherwise look plausible.
        """
        tx = tx_with(top_instructions=[burn_instruction(CHARLIE, 100, program_id="11111111111111111111111111111111")])
        self.assertEqual(decode.find_burns(tx, CHARLIE), [])

    def test_burn_checked_is_found_on_the_same_terms(self):
        tx = tx_with(top_instructions=[burn_instruction(CHARLIE, 77, checked=True)])
        burns = decode.find_burns(tx, CHARLIE)
        self.assertEqual(len(burns), 1)
        self.assertEqual(burns[0]["type"], "burnChecked")

    def test_burn_for_a_different_mint_is_not_returned(self):
        tx = tx_with(top_instructions=[burn_instruction(OTHER_MINT, 100)])
        self.assertEqual(decode.find_burns(tx, CHARLIE), [])

    def test_nested_burn_in_inner_instructions_is_found_with_top_index(self):
        tx = tx_with(
            top_instructions=[non_burn_instruction(), {"programId": "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"}],
            inner=[{"index": 1, "instructions": [burn_instruction(CHARLIE, 999)]}],
        )
        burns = decode.find_burns(tx, CHARLIE)
        self.assertEqual(len(burns), 1)
        self.assertEqual(burns[0]["top_index"], 1)

    def test_amount_arrives_as_string_and_is_returned_as_int(self):
        tx = tx_with(top_instructions=[burn_instruction(CHARLIE, 123_456_789)])
        burns = decode.find_burns(tx, CHARLIE)
        self.assertIsInstance(burns[0]["amount"], int)
        self.assertEqual(burns[0]["amount"], 123_456_789)

    def test_two_burns_in_one_transaction_get_distinct_instruction_indexes(self):
        tx = tx_with(top_instructions=[burn_instruction(CHARLIE, 1), burn_instruction(CHARLIE, 2)])
        burns = decode.find_burns(tx, CHARLIE)
        self.assertEqual(len(burns), 2)
        self.assertNotEqual(burns[0]["instruction_index"], burns[1]["instruction_index"])


# -- program_data_lines -------------------------------------------------------
class TestProgramDataLines(unittest.TestCase):
    def test_extracts_the_base64_payload(self):
        payload = boost_event_payload()
        tx = tx_with(log_messages=[program_data_log(payload), "Program log: something else"])
        lines = list(decode.program_data_lines(tx))
        self.assertEqual(len(lines), 1)
        self.assertEqual(base64.b64decode(lines[0]), bytes(payload))


# -- decode_boost_event --------------------------------------------------------
class TestDecodeBoostEvent(unittest.TestCase):
    def test_decodes_mint_authority_sol_and_tokens_at_pinned_offsets(self):
        payload = boost_event_payload(quote_used=900_000, base_burned=1_500_000_000)
        b64 = base64.b64encode(bytes(payload)).decode()
        event = decode.decode_boost_event(b64)
        self.assertIsNotNone(event)
        self.assertEqual(event["mint"], CHARLIE)
        self.assertEqual(event["bonding_curve"], BONDING_CURVE)
        self.assertEqual(event["pool"], POOL)
        self.assertEqual(event["authority"], AUTHORITY)
        self.assertEqual(event["sol_spent"], 900_000)
        self.assertEqual(event["tokens_burned"], 1_500_000_000)

    def test_pinned_charlie_boost_regression_vector(self):
        """RESEARCH.md Q8: $CHARLIE's boost totals, pinned as a regression
        vector. Boost-caused supply destruction, NOT protocol-attributed
        (D-11) -- pump's boost authority did it, not our crank.
        """
        payload = boost_event_payload(base_burned=CHARLIE_BOOST_TOKENS_BURNED)
        b64 = base64.b64encode(bytes(payload)).decode()
        event = decode.decode_boost_event(b64)
        self.assertEqual(event["tokens_burned"], CHARLIE_BOOST_TOKENS_BURNED)

    def test_buy_event_decodes_to_none(self):
        b64 = base64.b64encode(bytes(buy_event_payload())).decode()
        self.assertIsNone(decode.decode_boost_event(b64))

    def test_truncated_payload_raises_rather_than_reading_past_the_buffer(self):
        truncated = decode.DISC_BOOST_BUY_AND_BURN + bytes(10)  # far short of 168
        b64 = base64.b64encode(truncated).decode()
        with self.assertRaises(DecodeError):
            decode.decode_boost_event(b64)


# -- decode_create_event -------------------------------------------------------
class TestDecodeCreateEvent(unittest.TestCase):
    def test_decodes_mint_symbol_and_token_total_supply(self):
        payload = create_event_payload(token_total_supply=CHARLIE_INITIAL_SUPPLY)
        b64 = base64.b64encode(bytes(payload)).decode()
        event = decode.decode_create_event(b64)
        self.assertIsNotNone(event)
        self.assertEqual(event["mint"], CHARLIE)
        self.assertEqual(event["symbol"], "CHARLIE")
        self.assertEqual(event["token_total_supply"], CHARLIE_INITIAL_SUPPLY)

    def test_over_long_declared_string_length_raises_rather_than_allocating(self):
        payload = bytearray(decode.DISC_CREATE_EVENT)
        payload += (1_000_000).to_bytes(4, "little")  # declares 1,000,000 bytes of "name"
        payload += b"only a few bytes follow"          # buffer nowhere near that long
        b64 = base64.b64encode(bytes(payload)).decode()
        with self.assertRaises(DecodeError):
            decode.decode_create_event(b64)

    def test_non_matching_discriminator_decodes_to_none(self):
        b64 = base64.b64encode(bytes(buy_event_payload())).decode()
        self.assertIsNone(decode.decode_create_event(b64))


class FakeSupplyRpc:
    """Serves `signatures_for_address` pages in order, one page per call
    (`derive_initial_supply` always requests `pages=1`), regardless of the
    `before` cursor -- matching `tests/test_scan.py`'s `FakeScanRpc` style.
    """

    def __init__(self, pages, transactions, unfetchable=frozenset()):
        self._pages = list(pages)
        self._call_count = 0
        self._transactions = transactions
        self._unfetchable = unfetchable
        self.transaction_calls = 0

    def signatures_for_address(self, address, before=None, until=None, limit=1000):
        if self._call_count >= len(self._pages):
            return []
        page = self._pages[self._call_count]
        self._call_count += 1
        return page

    def transaction(self, signature):
        self.transaction_calls += 1
        if signature in self._unfetchable:
            return None
        return self._transactions.get(signature)


def evidence_db(tmp_dir: str) -> Evidence:
    return Evidence(Path(tmp_dir) / "evidence.db")


# -- derive_initial_supply (EVID-07/EVID-08) ----------------------------------
class TestDeriveInitialSupply(unittest.TestCase):
    def test_charlie_fixture_derives_the_chain_recorded_supply(self):
        """1,000,000,000,000,000 raw units -- read off the chain's own
        CreateEvent, not assumed (EVID-07).
        """
        with tempfile.TemporaryDirectory() as tmp:
            create_tx = tx_with(
                log_messages=[program_data_log(create_event_payload(token_total_supply=CHARLIE_INITIAL_SUPPLY))]
            )
            other_tx = tx_with(log_messages=["Program log: Instruction: Buy"])
            rpc = FakeSupplyRpc(
                pages=[[
                    {"signature": "sig-2", "err": None, "slot": 2, "blockTime": 2},
                    {"signature": "sig-1", "err": None, "slot": 1, "blockTime": 1},
                ]],
                transactions={"sig-1": create_tx, "sig-2": other_tx},
            )
            evidence = evidence_db(tmp)
            row = derive_initial_supply(rpc, evidence, CHARLIE, limit=5)
            evidence.close()

            self.assertIsNotNone(row)
            self.assertEqual(row["raw_supply"], CHARLIE_INITIAL_SUPPLY)
            self.assertEqual(row["decimals"], 6)
            self.assertEqual(row["creation_signature"], "sig-1")
            self.assertIsNone(row["unchecked_reason"])

    def test_second_derivation_performs_no_additional_rpc_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            create_tx = tx_with(
                log_messages=[program_data_log(create_event_payload(token_total_supply=CHARLIE_INITIAL_SUPPLY))]
            )
            rpc = FakeSupplyRpc(
                pages=[[{"signature": "sig-1", "err": None, "slot": 1, "blockTime": 1}]],
                transactions={"sig-1": create_tx},
            )
            evidence = evidence_db(tmp)
            first = derive_initial_supply(rpc, evidence, CHARLIE, limit=5)
            calls_after_first = rpc._call_count
            tx_calls_after_first = rpc.transaction_calls

            second = derive_initial_supply(rpc, evidence, CHARLIE, limit=5)
            evidence.close()

            self.assertEqual(second, first)
            self.assertEqual(rpc._call_count, calls_after_first)
            self.assertEqual(rpc.transaction_calls, tx_calls_after_first)

    def test_exhausted_without_create_event_writes_unchecked_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            no_create_tx = tx_with(log_messages=["Program log: nothing relevant"])
            rpc = FakeSupplyRpc(
                pages=[[{"signature": "sig-1", "err": None, "slot": 1, "blockTime": 1}]],
                transactions={"sig-1": no_create_tx},
            )
            evidence = evidence_db(tmp)
            row = derive_initial_supply(rpc, evidence, OTHER_MINT, limit=5)
            evidence.close()

            self.assertIsNotNone(row)
            self.assertIsNone(row["raw_supply"])
            self.assertIsNotNone(row["unchecked_reason"])
            self.assertIn("CreateEvent", row["unchecked_reason"])

    def test_unfinished_walk_writes_no_row_at_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            rpc = FakeSupplyRpc(
                pages=[[{"signature": "sig-1", "err": None, "slot": 1, "blockTime": 1}]],
                transactions={},
                unfetchable={"sig-1"},
            )
            evidence = evidence_db(tmp)
            row = derive_initial_supply(rpc, evidence, OTHER_MINT, limit=5)
            stored = evidence.initial_supply_for(OTHER_MINT)
            evidence.close()

            self.assertIsNone(row)
            self.assertIsNone(stored)


if __name__ == "__main__":
    unittest.main()

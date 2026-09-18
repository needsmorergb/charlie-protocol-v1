"""USDC pairs, Custom Pairs, Holder Rewards and pump's admin CTO.

pump added all four between May and September 2026. Every offset the code
reads for them is recomputed here from pump's published IDL (vendored in
`idl/pump-public-docs/pump.extract.json`), so a number typed wrong into `indexer/pump.py` or
`indexer/decode.py` fails here instead of on a real coin. CUSTOM-PAIRS.md is
the design these tests hold the code to.
"""

from __future__ import annotations

import base64
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from indexer import decode, enroll, pump  # noqa: E402
from indexer.base58 import encode, pubkey_bytes  # noqa: E402

IDL = json.loads((ROOT / "idl" / "pump-public-docs" / "pump.extract.json").read_text())

MINT = "JAMXU2JLraZ3RUhbgc3ttYPc18Kx4ojCnC56XR2zpump"
ADMIN = "Chx6EJ1QLRnhiyQHfpNNyiEWma8XPazbELPanPff4Nuj"
OPS = "4Nd1mBQtrMJVYVfKf2PJy9NZUZdTAsp7D4xWLs4gDB4T"
BURN = "1nc1nerator11111111111111111111111111111111"
TOLL = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
REAL_TOLL = enroll.legs.TOLL_DESTINATION
BLOCKHASH = "11111111111111111111111111111111"
# Tokenized Nvidia's xStocks mint stands in for any Custom Pair; the exact
# address does not matter, only that it is neither default, wSOL nor USDC.
XSTOCK = "Xsc9qvGR1efVDFGLrVsmkzv3qi45LTBjeUKSPmx9qEh"

_SIZES = {"u8": 1, "bool": 1, "u16": 2, "u64": 8, "i64": 8, "u128": 16, "i128": 16, "pubkey": 32}


def _layout(type_name):
    for t in IDL["types"]:
        if t["name"] == type_name:
            cursor, out = 8, {}
            for field in t["type"]["fields"]:
                out[field["name"]] = cursor
                cursor += _SIZES[field["type"]]
            return out, cursor
    raise KeyError(type_name)


def _curve_bytes(*, quote=None, fee_bps=0, holder=False, cashback=False, length=None):
    data = bytearray(pump.DISC_BONDING_CURVE + bytes(40) + b"\x00" + pubkey_bytes(ADMIN))
    data += b"\x00" + (b"\x01" if cashback else b"\x00")
    data += pubkey_bytes(quote) if quote else bytes(32)
    data += fee_bps.to_bytes(8, "little") + b"\x00" + (b"\x01" if holder else b"\x00")
    data += bytes(151 - len(data))
    return bytes(data[:length] if length else data)


def _read(data):
    account = {"owner": pump.PUMP_PROGRAM, "data": [base64.b64encode(data).decode(), "base64"]}

    class _Rpc:
        def accounts(self, _addresses):
            return [account]

    return pump.read_bonding_curve(_Rpc(), MINT)


class TestOffsetsComeFromTheIdl(unittest.TestCase):
    def test_bonding_curve_offsets(self):
        layout, _end = _layout("BondingCurve")
        self.assertEqual(layout["is_cashback_coin"], pump.CASHBACK_FLAG_OFFSET)
        self.assertEqual(layout["quote_mint"], pump.QUOTE_MINT_OFFSET)
        self.assertEqual(layout["creator_fee_bps"], pump.CREATOR_FEE_BPS_OFFSET)
        self.assertEqual(layout["is_holder_reward"], pump.HOLDER_REWARD_OFFSET)

    def test_admin_cto_event_layout_and_discriminator(self):
        layout, end = _layout("AdminCtoEvent")
        self.assertEqual(end, decode._ADMIN_CTO_EVENT_LEN)
        self.assertEqual(layout["sharing_config_reset"], 194)
        self.assertEqual(layout["pool_updated"], 203)
        idl_disc = next(bytes(e["discriminator"]) for e in IDL["events"] if e["name"] == "AdminCtoEvent")
        self.assertEqual(idl_disc, decode.DISC_ADMIN_CTO_EVENT)
        self.assertEqual(decode.anchor_discriminator("AdminCtoEvent"), decode.DISC_ADMIN_CTO_EVENT)

    def test_not_yet_sampled_is_declared(self):
        # Flipping this needs the mainnet sample committed beside it.
        self.assertFalse(pump.QUOTE_FIELDS_SAMPLED)


class TestReadingTheCurve(unittest.TestCase):
    def test_default_quote_is_sol(self):
        curve = _read(_curve_bytes())
        self.assertIsNone(curve.quote_mint)
        self.assertEqual(curve.quote, "sol")

    def test_wsol_reads_as_sol(self):
        self.assertEqual(_read(_curve_bytes(quote=pump.WSOL_MINT)).quote, "sol")

    def test_usdc_and_custom(self):
        self.assertEqual(_read(_curve_bytes(quote=pump.USDC_MINT)).quote, "usdc")
        custom = _read(_curve_bytes(quote=XSTOCK, fee_bps=45))
        self.assertEqual(custom.quote, "custom")
        self.assertEqual(custom.quote_mint, XSTOCK)
        self.assertEqual(custom.creator_fee_bps, 45)

    def test_holder_reward(self):
        self.assertTrue(_read(_curve_bytes(holder=True)).holder_reward)
        self.assertFalse(_read(_curve_bytes()).holder_reward)

    def test_old_short_accounts(self):
        # 83 bytes: before USDC pairs. SOL, no custom fee, never holder rewards.
        curve = _read(_curve_bytes(length=83))
        self.assertEqual((curve.quote, curve.creator_fee_bps, curve.holder_reward), ("sol", 0, False))
        # 115 bytes: quote mint present, nothing after it.
        curve = _read(_curve_bytes(quote=pump.USDC_MINT, length=115))
        self.assertEqual((curve.quote, curve.creator_fee_bps, curve.holder_reward), ("usdc", 0, False))

    def test_cashback_byte_unchanged(self):
        self.assertTrue(_read(_curve_bytes(cashback=True)).cashback)


class _Curve:
    def __init__(self, quote_mint=None, holder_reward=False, cashback=False):
        self.mint, self.creator, self.graduated = MINT, ADMIN, False
        self.quote_mint, self.holder_reward, self.cashback = quote_mint, holder_reward, cashback


def _split(burn=True):
    rows = [enroll.Share(TOLL, enroll.TOLL_BPS)]
    if burn:
        rows += [enroll.Share(BURN, 2000), enroll.Share(ADMIN, 10000 - enroll.TOLL_BPS - 2000)]
    else:
        rows += [enroll.Share(ADMIN, 10000 - enroll.TOLL_BPS)]
    return rows


class TestPreflightRefusals(unittest.TestCase):
    def setUp(self):
        enroll.legs.TOLL_DESTINATION = TOLL
        self._open = enroll.NON_SOL_ENROLLMENT_OPEN

    def tearDown(self):
        enroll.legs.TOLL_DESTINATION = REAL_TOLL
        enroll.NON_SOL_ENROLLMENT_OPEN = self._open

    def test_sol_is_unchanged(self):
        enroll.preflight(None, ADMIN, _split(), curve=_Curve())

    def test_holder_rewards_refused(self):
        with self.assertRaisesRegex(enroll.EnrollError, "Holder Rewards"):
            enroll.preflight(None, ADMIN, _split(), curve=_Curve(holder_reward=True))

    def test_custom_pair_refused(self):
        with self.assertRaisesRegex(enroll.EnrollError, "custom pairs"):
            enroll.preflight(None, ADMIN, _split(burn=False), curve=_Curve(quote_mint=XSTOCK))

    def test_usdc_incinerator_row_refused_even_when_open(self):
        enroll.NON_SOL_ENROLLMENT_OPEN = True
        with self.assertRaisesRegex(enroll.EnrollError, "not burned"):
            enroll.preflight(None, ADMIN, _split(), curve=_Curve(quote_mint=enroll.USDC_MINT))

    def test_usdc_closed_by_default(self):
        self.assertFalse(enroll.NON_SOL_ENROLLMENT_OPEN)
        with self.assertRaisesRegex(enroll.EnrollError, "not open yet"):
            enroll.preflight(None, ADMIN, _split(burn=False), curve=_Curve(quote_mint=enroll.USDC_MINT))

    def test_usdc_passes_when_open(self):
        enroll.NON_SOL_ENROLLMENT_OPEN = True
        enroll.preflight(None, ADMIN, _split(burn=False), curve=_Curve(quote_mint=enroll.USDC_MINT))

    def test_quote_accounts(self):
        self.assertEqual(enroll.quote_accounts(_Curve())["quote_mint"], enroll.WSOL_MINT)
        self.assertEqual(enroll.quote_accounts(_Curve(quote_mint=enroll.USDC_MINT)),
                         {"quote_mint": enroll.USDC_MINT, "token_program": enroll.TOKEN_PROGRAM})
        with self.assertRaises(enroll.EnrollError):
            enroll.quote_accounts(_Curve(quote_mint=XSTOCK))


class TestUsdcTransaction(unittest.TestCase):
    def test_remaining_accounts_sol_is_wallets_only(self):
        self.assertEqual(enroll.remaining_accounts([ADMIN, OPS]),
                         [(ADMIN, False, True), (OPS, False, True)])

    def test_remaining_accounts_usdc_is_wallets_then_atas_in_order(self):
        rows = enroll.remaining_accounts([ADMIN, OPS], quote_mint=enroll.USDC_MINT)
        self.assertEqual([r[0] for r in rows], [
            ADMIN, OPS,
            enroll.associated_token_address(ADMIN, enroll.USDC_MINT),
            enroll.associated_token_address(OPS, enroll.USDC_MINT),
        ])

    def test_usdc_accounts_carry_the_quote(self):
        metas = enroll.accounts_for(MINT, ADMIN, quote_mint=enroll.USDC_MINT)
        self.assertEqual(metas[14][0], enroll.USDC_MINT)          # quote_mint, IDL position 15
        config = enroll.sharing_config_address(MINT)
        vault = enroll._pda([b"creator-vault", pubkey_bytes(config)], enroll.PUMP_PROGRAM)
        self.assertEqual(metas[8][0], enroll.associated_token_address(vault, enroll.USDC_MINT))

    def test_graduated_usdc_create_passes_the_usdc_pool(self):
        metas = enroll.create_accounts_for(MINT, ADMIN, graduated=True, quote_mint=enroll.USDC_MINT)
        self.assertEqual(metas[10][0], enroll.canonical_pool_address(MINT, enroll.USDC_MINT))
        self.assertNotEqual(metas[10][0], enroll.canonical_pool_address(MINT))

    def test_sol_messages_are_byte_identical_to_main(self):
        # The SOL path was simulated against mainnet; this branch must not move
        # a byte of it. Hashes generated from main at f6e922e, before any of
        # this branch, for: create, create on a graduated coin, update only.
        import hashlib
        split = [enroll.Share(TOLL, 2500), enroll.Share(ADMIN, 7500)]
        built = [
            enroll.enrollment_message(MINT, ADMIN, split, BLOCKHASH, create=True),
            enroll.enrollment_message(MINT, ADMIN, split, BLOCKHASH, create=True, graduated=True),
            enroll.enrollment_message(MINT, ADMIN, split, BLOCKHASH, create=False, current=[ADMIN, TOLL]),
        ]
        self.assertEqual([hashlib.sha256(m).hexdigest() for m in built], [
            "489e143de05aa36eefb0025556886aa97d1df3a2e0664ef3569324ae41ca48e2",
            "5326304d7b2b8350f437ab5aff40ee1a5d8723161b52fb4f79152898047f1b0c",
            "cb6b6c8859fad53831f57f48ea889e9e304a65491ece15333fe1728b60bb3484",
        ])

    def test_usdc_create_message_names_usdc_and_the_creator_ata(self):
        split = [enroll.Share(TOLL, enroll.TOLL_BPS), enroll.Share(ADMIN, 10000 - enroll.TOLL_BPS)]
        message = enroll.enrollment_message(MINT, ADMIN, split, BLOCKHASH, create=True,
                                            quote_mint=enroll.USDC_MINT)
        self.assertIn(pubkey_bytes(enroll.USDC_MINT), message)
        self.assertIn(pubkey_bytes(enroll.associated_token_address(ADMIN, enroll.USDC_MINT)), message)


def _cto_payload(mint=MINT, reset=True):
    raw = bytearray(decode.DISC_ADMIN_CTO_EVENT)
    raw += (1_758_000_000).to_bytes(8, "little", signed=True)
    for key in (OPS, mint, ADMIN, ADMIN, OPS):
        raw += pubkey_bytes(key)
    raw += b"\x01\x00" + (0).to_bytes(8, "little") + (0).to_bytes(8, "little")
    raw += (b"\x01" if reset else b"\x00") + (123).to_bytes(8, "little") + b"\x00"
    return base64.b64encode(bytes(raw)).decode()


class TestAdminCto(unittest.TestCase):
    def test_decodes(self):
        event = decode.decode_admin_cto_event(_cto_payload())
        self.assertEqual(event["mint"], MINT)
        self.assertEqual(event["new_creator"], OPS)
        self.assertTrue(event["is_holder_reward"])
        self.assertTrue(event["sharing_config_reset"])
        self.assertEqual(event["swept_to_holder_vault"], 123)

    def test_other_events_are_none(self):
        other = base64.b64encode(decode.DISC_BUY_EVENT + bytes(300)).decode()
        self.assertIsNone(decode.decode_admin_cto_event(other))

    def test_short_payload_raises(self):
        short = base64.b64encode(decode.DISC_ADMIN_CTO_EVENT + bytes(20)).decode()
        with self.assertRaises(pump.DecodeError):
            decode.decode_admin_cto_event(short)

    def test_find_in_a_transaction_filters_by_mint(self):
        tx = {"meta": {"logMessages": [
            "Program log: Instruction: AdminCto",
            f"Program data: {_cto_payload()}",
            f"Program data: {_cto_payload(mint=OPS)}",
        ]}}
        self.assertEqual(len(decode.find_admin_cto(tx)), 2)
        self.assertEqual([e["mint"] for e in decode.find_admin_cto(tx, mints=[MINT])], [MINT])


if __name__ == "__main__":
    unittest.main()

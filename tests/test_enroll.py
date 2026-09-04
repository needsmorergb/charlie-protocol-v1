"""The fee-split transaction builder.

Every constant here was read from pump's own on-chain Anchor IDL, and the
whole message was proven by simulating it against mainnet, which returned
`err: None`. These tests pin the parts of that result a later edit could
silently break -- above all the ACCOUNT ORDER, which no reader can check by
eye and which the runtime reads positionally.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from indexer import enroll  # noqa: E402
from indexer.base58 import encode, pubkey_bytes  # noqa: E402
from indexer.curve import find_program_address  # noqa: E402

MINT = "JAMXU2JLraZ3RUhbgc3ttYPc18Kx4ojCnC56XR2zpump"
ADMIN = "Chx6EJ1QLRnhiyQHfpNNyiEWma8XPazbELPanPff4Nuj"
# Solana's incinerator. The runtime removes lamports credited here from the
# total supply at the end of the block, which is the only address on the chain
# where SOL is destroyed rather than merely made unspendable.
BURN = "1nc1nerator11111111111111111111111111111111"
BLOCKHASH = "11111111111111111111111111111111"


class _Config:
    def __init__(self, admin=ADMIN, revoked=False, shareholders=((ADMIN, 10000),)):
        self.address = enroll.sharing_config_address(MINT)
        self.admin = admin
        self.admin_revoked = revoked
        self.shareholders = shareholders


def _split():
    return [enroll.Share(BURN, 2000), enroll.Share(ADMIN, 8000)]


class TestBurnDestination(unittest.TestCase):
    """The default destination has to actually burn.

    An address with no private key parks SOL: it cannot be spent, but the
    supply is unchanged and the lamports still exist. Only the incinerator
    reduces the supply, and only supply reduction is deflation.
    """

    def test_the_default_is_the_incinerator(self):
        from indexer import enroll_page
        self.assertIn("1nc1nerator11111111111111111111111111111111", enroll_page.render(now=1))

    def test_the_page_does_not_offer_a_merely_unspendable_address(self):
        from indexer import enroll_page
        page = enroll_page.render(now=1)
        self.assertNotIn("burn11111111111111111111111111111111111111", page)

    def test_the_page_says_the_supply_falls_rather_than_the_sol_being_stuck(self):
        from indexer import enroll_page
        page = enroll_page.render(now=1)
        self.assertIn("removed from", page)
        self.assertIn("total supply", page)

    def test_a_split_to_the_incinerator_builds(self):
        """Simulated against mainnet with err: None before this was written."""
        data = enroll.instruction_data([enroll.Share(BURN, 2000), enroll.Share(ADMIN, 8000)])
        self.assertEqual(data[12:44], pubkey_bytes(BURN))


class TestDerivations(unittest.TestCase):
    def test_sharing_config_matches_the_chain(self):
        """Checked against mainnet: this coin's bonding curve names exactly
        this address as its creator.
        """
        self.assertEqual(
            enroll.sharing_config_address("CfBYu1dsy6nC3oiYmxE3Kxds6auwpoxcErvSMVykVEPH"),
            "37GmYdwq8D18Cc3KxeqXEC2gqqHj1MXCr5fNEDt9hkXL",
        )

    def test_global_is_derived_under_pump_not_the_fee_program(self):
        """The IDL names the program for this PDA explicitly. Deriving it
        under the enclosing program instead produced an address that does not
        exist, and the simulation said exactly that: AccountNotInitialized,
        naming `global`.
        """
        accounts = [a for a, _s, _w in enroll.accounts_for(MINT, ADMIN)]
        self.assertIn(find_program_address([b"global"], enroll.PUMP_PROGRAM)[0], accounts)
        self.assertNotIn(find_program_address([b"global"], enroll.FEE_SHARE_PROGRAM)[0], accounts)


class TestInstructionData(unittest.TestCase):
    def test_discriminator_is_the_one_the_program_published(self):
        self.assertEqual(enroll.instruction_data(_split())[:8].hex(), "6ffb31064e4e6a12")

    def test_vec_is_length_prefixed_then_34_bytes_each(self):
        data = enroll.instruction_data(_split())
        self.assertEqual(int.from_bytes(data[8:12], "little"), 2)
        self.assertEqual(len(data), 8 + 4 + 2 * 34)
        self.assertEqual(data[12:44], pubkey_bytes(BURN))
        self.assertEqual(int.from_bytes(data[44:46], "little"), 2000)


class TestValidation(unittest.TestCase):
    """Rejected here costs the dev nothing. Rejected on chain costs a fee AND
    can consume the single update pump allows.
    """

    def test_must_total_exactly_10000(self):
        with self.assertRaises(enroll.EnrollError) as caught:
            enroll.validate([enroll.Share(BURN, 2000), enroll.Share(ADMIN, 7000)])
        self.assertIn("9000", str(caught.exception))
        self.assertIn("1000 bps short", str(caught.exception))

    def test_rejects_a_duplicate_destination(self):
        with self.assertRaises(enroll.EnrollError):
            enroll.validate([enroll.Share(BURN, 5000), enroll.Share(BURN, 5000)])

    def test_rejects_a_zero_share_rather_than_sending_it(self):
        with self.assertRaises(enroll.EnrollError):
            enroll.validate([enroll.Share(BURN, 0), enroll.Share(ADMIN, 10000)])

    def test_rejects_an_invalid_address(self):
        with self.assertRaises(enroll.EnrollError):
            enroll.validate([enroll.Share("not-an-address", 10000)])

    def test_rejects_more_destinations_than_pump_holds(self):
        rows = [enroll.Share(encode(bytes([i]) + b"\x01" * 31), 1000) for i in range(10)]
        with self.assertRaises(enroll.EnrollError):
            enroll.validate(rows)


class TestPreflight(unittest.TestCase):
    def test_a_revoked_config_is_refused_with_the_reason(self):
        with self.assertRaises(enroll.EnrollError) as caught:
            enroll.preflight(_Config(revoked=True), ADMIN, _split())
        self.assertIn("permanent", str(caught.exception))

    def test_a_wallet_that_is_not_the_admin_is_refused_and_told_who_is(self):
        with self.assertRaises(enroll.EnrollError) as caught:
            enroll.preflight(_Config(), BURN, _split())
        self.assertIn(ADMIN, str(caught.exception))

    def test_a_coin_with_no_config_says_so(self):
        with self.assertRaises(enroll.EnrollError) as caught:
            enroll.preflight(None, ADMIN, [enroll.Share(ADMIN, 10000)])
        self.assertIn("no pump fee-sharing config", str(caught.exception))

    def test_ownership_is_the_admin_key_and_nothing_else(self):
        self.assertTrue(enroll.owns(_Config(), ADMIN))
        self.assertFalse(enroll.owns(_Config(), BURN))
        self.assertFalse(enroll.owns(None, ADMIN))


class TestMessage(unittest.TestCase):
    """The exact shape that simulated cleanly against mainnet."""

    def _message(self, current=(ADMIN,)):
        return enroll.message(MINT, ADMIN, _split(), BLOCKHASH, current=list(current))

    def test_exactly_one_signer_and_it_sorts_first(self):
        msg = self._message()
        self.assertEqual(msg[0], 1)
        self.assertEqual(msg[4:36], pubkey_bytes(ADMIN))

    def test_account_order_is_the_idl_order(self):
        """The runtime reads accounts positionally, so a list right in content
        and wrong in order is a different instruction.
        """
        metas = enroll.accounts_for(MINT, ADMIN)
        names = [a for a, _s, _w in metas]
        self.assertEqual(names[1], enroll.FEE_SHARE_PROGRAM)
        self.assertEqual(names[2], ADMIN)
        self.assertEqual(names[4], MINT)
        self.assertEqual(names[5], enroll.sharing_config_address(MINT))
        self.assertEqual(names[9], enroll.SYSTEM_PROGRAM)
        self.assertEqual(names[10], enroll.PUMP_PROGRAM)
        self.assertEqual(names[12], enroll.PUMP_AMM_PROGRAM)
        self.assertEqual(names[16], enroll.ASSOCIATED_TOKEN_PROGRAM)
        self.assertEqual(len(metas), 19)

    def test_only_the_authority_signs(self):
        signers = [a for a, signer, _w in enroll.accounts_for(MINT, ADMIN) if signer]
        self.assertEqual(signers, [ADMIN])

    def test_current_shareholders_are_appended_as_remaining_accounts(self):
        """No IDL describes remaining accounts. With none the program answers
        NotEnoughRemainingAccounts; with the CURRENT shareholders it succeeds;
        with the new ones it fails 6020. Established by simulation.
        """
        self.assertGreater(len(self._message()), len(self._message(current=())))

    def test_the_instruction_data_is_carried_verbatim(self):
        self.assertIn(enroll.instruction_data(_split()), self._message())

    def test_the_blockhash_is_present(self):
        self.assertIn(pubkey_bytes(BLOCKHASH), self._message())

    def test_an_invalid_split_never_reaches_a_message(self):
        with self.assertRaises(enroll.EnrollError):
            enroll.message(MINT, ADMIN, [enroll.Share(BURN, 9999)], BLOCKHASH, current=[ADMIN])

class _Curve:
    """Only the field the refusal turns on. `cashback` is three-valued in
    `pump.BondingCurve` and the tests below cover all three, because absent
    is not off."""

    def __init__(self, cashback=False):
        self.mint = MINT
        self.graduated = False
        self.cashback = cashback


class TestCashbackIsRefused(unittest.TestCase):
    """pump's Trader Cashback routes the WHOLE creator fee to traders, so a
    cashback coin's every leg is zero: not just the protocol's share but the
    SOL burn, the dev's own buy-and-burn and their ops wallet.

    Enrolling one is worse than pointless. pump allows a split to be updated
    exactly once, so it spends that single permanent change on a split which
    can never pay out, and leaves the coin `admin_revoked` with no recourse.
    """

    def test_a_cashback_coin_is_refused_before_a_wallet_opens(self):
        with self.assertRaises(enroll.EnrollError) as caught:
            enroll.preflight(_Config(), ADMIN, _split(), curve=_Curve(cashback=True))
        self.assertIn("Trader Cashback", str(caught.exception))

    def test_a_coin_without_cashback_proceeds(self):
        enroll.preflight(_Config(), ADMIN, _split(), curve=_Curve(cashback=False))

    def test_an_unreadable_flag_is_not_read_as_off_or_as_on(self):
        # None means the bonding curve predates the field. Refusing here would
        # assert cashback from an absent byte, which is the inverse of the
        # mistake the site's copy already refuses to make; the page warns and
        # lets the dev, who knows whether fees have ever arrived, decide.
        enroll.preflight(_Config(), ADMIN, _split(), curve=_Curve(cashback=None))

    def test_the_curve_is_optional_so_older_callers_still_work(self):
        enroll.preflight(_Config(), ADMIN, _split())

if __name__ == "__main__":
    unittest.main()

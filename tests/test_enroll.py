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
# A stand-in for the protocol's collection address. Every test that reaches
# `preflight` patches `enroll.legs.TOLL_DESTINATION` to it, so the tests hold
# whatever the real one is set to; a None refuses everything -- which is its
# own test below.
TOLL = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
REAL_TOLL = enroll.legs.TOLL_DESTINATION
# A creator that is not the admin, for the create path's refusals.
STRANGER = "So11111111111111111111111111111111111111112"


class _Config:
    def __init__(self, admin=ADMIN, revoked=False, shareholders=((ADMIN, 10000),)):
        self.address = enroll.sharing_config_address(MINT)
        self.admin = admin
        self.admin_revoked = revoked
        self.shareholders = shareholders


def _split():
    return [enroll.Share(TOLL, enroll.TOLL_BPS), enroll.Share(BURN, 2000),
            enroll.Share(ADMIN, 7500)]


def setUpModule():
    enroll.legs.TOLL_DESTINATION = TOLL


def tearDownModule():
    enroll.legs.TOLL_DESTINATION = REAL_TOLL


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
        self.assertEqual(int.from_bytes(data[8:12], "little"), len(_split()))
        self.assertEqual(len(data), 8 + 4 + len(_split()) * 34)
        # First record: the toll. Second: the incinerator at 20%.
        self.assertEqual(data[12:44], pubkey_bytes(TOLL))
        self.assertEqual(int.from_bytes(data[44:46], "little"), enroll.TOLL_BPS)
        self.assertEqual(data[46:78], pubkey_bytes(BURN))
        self.assertEqual(int.from_bytes(data[78:80], "little"), 2000)


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
    """`cashback` is three-valued in `pump.BondingCurve` and the tests cover
    all three, because absent is not off. `creator` is the wallet that
    launched the coin, which is what the create path's ownership turns on."""

    def __init__(self, cashback=False, creator=ADMIN, graduated=False):
        self.mint = MINT
        self.graduated = graduated
        self.cashback = cashback
        self.creator = creator


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

class TestTheToll(unittest.TestCase):
    """The protocol's share is in every enrolled split, at its fixed rate, or
    nothing is built. No program enforces this today; the page does, and
    these are what make the page do it."""

    def test_a_split_without_the_toll_is_refused(self):
        with self.assertRaises(enroll.EnrollError) as caught:
            enroll.preflight(_Config(), ADMIN, [enroll.Share(BURN, 2000), enroll.Share(ADMIN, 8000)],
                             curve=_Curve())
        self.assertIn("protocol's share", str(caught.exception))
        self.assertIn(TOLL, str(caught.exception))

    def test_the_toll_at_the_wrong_rate_is_refused(self):
        wrong = [enroll.Share(TOLL, 1000), enroll.Share(ADMIN, 9000)]
        with self.assertRaises(enroll.EnrollError) as caught:
            enroll.preflight(_Config(), ADMIN, wrong, curve=_Curve())
        self.assertIn("fixed at 5%", str(caught.exception))

    def test_the_toll_at_its_rate_passes(self):
        enroll.preflight(_Config(), ADMIN, _split(), curve=_Curve())

    def test_a_malformed_split_is_reported_before_a_missing_toll(self):
        # 9999 bps is the fault the dev can see; a missing toll on top of it
        # would be noise until the total is right.
        with self.assertRaises(enroll.EnrollError) as caught:
            enroll.preflight(_Config(), ADMIN, [enroll.Share(ADMIN, 9999)], curve=_Curve())
        self.assertIn("10000", str(caught.exception))

    def test_nothing_is_built_while_the_destination_is_unset(self):
        """None is the shipped default until the address is configured, and
        it must refuse everything: an enrolment whose toll went nowhere
        would be worse than no enrolment at all.
        """
        enroll.legs.TOLL_DESTINATION = None
        try:
            with self.assertRaises(enroll.EnrollError) as caught:
                enroll.preflight(_Config(), ADMIN, _split(), curve=_Curve())
            self.assertIn("not open yet", str(caught.exception))
        finally:
            enroll.legs.TOLL_DESTINATION = TOLL

    def test_the_rate_is_five_percent(self):
        self.assertEqual(enroll.TOLL_BPS, 500)


class TestCreatingTheConfig(unittest.TestCase):
    """A coin with no fee-sharing config -- roughly 95% of launches -- is
    enrolled by creating one and setting the split in ONE transaction.

    Every fact here was measured against mainnet by the deploy repository's
    `trace` workflow (tools/simulate_create_config.py): the creator may
    create; a stranger is refused 6016; creation sets admin = creator and the
    shareholders to the creator at 100%; and an update appended to the same
    transaction succeeds, with `[creator]` as its remaining accounts.
    """

    def test_the_creator_may_enrol_a_config_less_coin(self):
        enroll.preflight(None, ADMIN, _split(), curve=_Curve(creator=ADMIN))

    def test_anyone_else_is_refused_and_told_who_the_creator_is(self):
        with self.assertRaises(enroll.EnrollError) as caught:
            enroll.preflight(None, ADMIN, _split(), curve=_Curve(creator=STRANGER))
        self.assertIn(STRANGER, str(caught.exception))
        self.assertIn("only its creator", str(caught.exception))

    def test_a_graduated_coin_without_a_config_is_refused_not_built_wrongly(self):
        """Creation for a graduated coin needs its AMM pool (fee-share error
        6019), which this does not build. Refusing is the honest answer;
        building with the absent-pool convention would fail on chain after
        the dev signed."""
        with self.assertRaises(enroll.EnrollError) as caught:
            enroll.preflight(None, ADMIN, _split(), curve=_Curve(creator=ADMIN, graduated=True))
        self.assertIn("graduated", str(caught.exception))

    def test_without_a_curve_there_is_nothing_to_decide_ownership_from(self):
        with self.assertRaises(enroll.EnrollError):
            enroll.preflight(None, ADMIN, _split())

    def test_cashback_is_refused_on_the_create_path_too(self):
        with self.assertRaises(enroll.EnrollError) as caught:
            enroll.preflight(None, ADMIN, _split(), curve=_Curve(creator=ADMIN, cashback=True))
        self.assertIn("Trader Cashback", str(caught.exception))

    def test_create_accounts_are_in_the_idl_order(self):
        metas = enroll.create_accounts_for(MINT, ADMIN)
        names = [a for a, _s, _w in metas]
        self.assertEqual(len(metas), 13)
        self.assertEqual(names[1], enroll.FEE_SHARE_PROGRAM)
        self.assertEqual(names[2], ADMIN)                                   # payer, signer
        self.assertEqual(names[4], MINT)
        self.assertEqual(names[5], enroll.sharing_config_address(MINT))
        self.assertEqual(names[6], enroll.SYSTEM_PROGRAM)
        self.assertEqual(names[7], enroll.bonding_curve_address(MINT))
        self.assertEqual(names[8], enroll.PUMP_PROGRAM)
        # `pool`: the program id, Anchor's absent-optional-account convention,
        # which is what the simulation passed for an un-graduated coin.
        self.assertEqual(names[10], enroll.FEE_SHARE_PROGRAM)
        self.assertEqual(names[11], enroll.PUMP_AMM_PROGRAM)
        self.assertEqual([a for a, signer, _w in metas if signer], [ADMIN])
        writable = [a for a, _s, w in metas if w]
        self.assertIn(enroll.sharing_config_address(MINT), writable)
        self.assertIn(enroll.bonding_curve_address(MINT), writable)

    def test_the_create_discriminator_is_the_idl_s(self):
        self.assertEqual(enroll.CREATE_FEE_SHARING_CONFIG.hex(), "c34e564c6f34fbd5")

    def _two(self):
        return enroll.enrolment_message(MINT, ADMIN, _split(), BLOCKHASH, create=True)

    def test_one_signature_carries_two_instructions(self):
        msg = self._two()
        self.assertEqual(msg[0], 1)                       # one signer
        self.assertEqual(msg[4:36], pubkey_bytes(ADMIN))  # and it sorts first
        # Instruction count sits right after the blockhash: header (3) +
        # compact len + 32 * n accounts + 32 blockhash.
        n = msg[3]
        self.assertEqual(msg[4 + 32 * n + 32], 2)

    def test_create_comes_before_the_update(self):
        msg = self._two()
        self.assertLess(msg.index(enroll.CREATE_FEE_SHARING_CONFIG),
                        msg.index(enroll.instruction_data(_split())))

    def test_the_update_s_remaining_account_is_the_creator(self):
        """After creation the config's only shareholder is the creator at
        100%, so that is what the update must be handed. Passing the NEW
        shareholders answers 6020."""
        _program, metas, _data = enroll.update_instruction(MINT, ADMIN, _split(), current=[ADMIN])
        self.assertEqual(metas[-1], (ADMIN, False, True))

    def test_without_create_it_is_the_one_instruction_message(self):
        one = enroll.enrolment_message(MINT, ADMIN, _split(), BLOCKHASH, create=False,
                                       current=[ADMIN])
        self.assertEqual(one, enroll.message(MINT, ADMIN, _split(), BLOCKHASH, current=[ADMIN]))

    def test_may_create_is_the_creator_of_an_ungraduated_coin(self):
        self.assertTrue(enroll.may_create(_Curve(creator=ADMIN), ADMIN))
        self.assertFalse(enroll.may_create(_Curve(creator=STRANGER), ADMIN))
        self.assertFalse(enroll.may_create(_Curve(creator=ADMIN, graduated=True), ADMIN))
        self.assertFalse(enroll.may_create(None, ADMIN))


if __name__ == "__main__":
    unittest.main()

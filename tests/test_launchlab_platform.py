"""The platform-only setup path: creator fee zero, and no program of ours.

LAUNCHLAB-RAIL.md's decision of 20 September 2026. The thing most worth a
test here is not the encoding -- mainnet simulation checks that -- it is the
zero. `launchlab_mainnet_setup.py` produces the 0.50% shape from
`CREATOR_FEE_RATE = 5_000`, and the whole point of this path is that it does
not.
"""

import re
import struct
import unittest
from pathlib import Path

from tools import launchlab_platform as lp

ROOT = Path(__file__).resolve().parents[1]
ADMIN = "8SvEu1bvkhgaSkZW4XHLzfw8djd748KAVHMwvkYGfyr8"


class TestTheCreatorFeeIsZero(unittest.TestCase):
    def test_the_constant_is_zero(self):
        self.assertEqual(lp.CREATOR_FEE_RATE, 0)

    def test_the_built_instruction_carries_zero(self):
        _, _, data = lp.create_platform_config(ADMIN, lp.DEFAULT_CPMM_CONFIG, "n", "w", "i")
        # discriminator, four u64s, then three borsh strings, then
        # creator_fee_rate and platform_vesting_scale.
        tail = data[-16:]
        creator_fee_rate, vesting = struct.unpack("<QQ", tail)
        self.assertEqual(creator_fee_rate, 0)
        self.assertEqual(vesting, 0)

    def test_the_abandoned_program_still_carries_the_old_rate(self):
        """Not a complaint about the Rust -- a reason this path exists.

        `launchlab/src/lib.rs` hardcodes the 0.50% creator rate, and
        `tools/launchlab_mainnet_setup.py` reaches it through the program's
        own instruction. If that constant is ever changed to zero, this test
        should be deleted along with the paragraph in LAUNCHLAB-RAIL.md that
        warns about it.
        """
        rust = (ROOT / "launchlab" / "src" / "lib.rs").read_text(encoding="utf-8")
        match = re.search(r"CREATOR_FEE_RATE:\s*u64\s*=\s*([0-9_]+)", rust)
        self.assertIsNotNone(match, "CREATOR_FEE_RATE is gone from launchlab/src/lib.rs")
        self.assertEqual(int(match.group(1).replace("_", "")), 5_000)


class TestItAgreesWithTheRust(unittest.TestCase):
    def test_the_discriminator_is_the_one_the_crate_uses(self):
        rust = (ROOT / "launchlab" / "src" / "raydium.rs").read_text(encoding="utf-8")
        match = re.search(r"DISC_CREATE_PLATFORM_CONFIG:\s*\[u8;\s*8\]\s*=\s*\[([^\]]+)\]", rust)
        self.assertIsNotNone(match)
        self.assertEqual(bytes(int(b) for b in match.group(1).split(",")), lp.DISC_CREATE_PLATFORM_CONFIG)

    def test_the_platform_config_pda_seeds_match(self):
        rust = (ROOT / "launchlab" / "src" / "raydium.rs").read_text(encoding="utf-8")
        self.assertIn('&[b"platform_config", platform_admin.as_ref()]', rust)
        self.assertEqual(lp.platform_config(ADMIN), "FNaAEhfh3BX9YiC8WsgwaU9PdKseCzZqgy25SoeZTNPc")

    def test_the_fee_vault_has_no_seed_prefix(self):
        """Seeds are the config and the quote mint, in that order, with no
        prefix -- the shape `platform_fee_vault` uses in raydium.rs."""
        rust = (ROOT / "launchlab" / "src" / "raydium.rs").read_text(encoding="utf-8")
        self.assertIn("&[platform_config.as_ref(), quote_mint.as_ref()]", rust)


class TestTheAccountList(unittest.TestCase):
    def test_every_wallet_role_is_the_admin(self):
        """The reason no program is needed: the fee wallet, the NFT wallet,
        the transfer-fee authority and the vesting wallet are all roles an
        ordinary key can hold."""
        program, metas, _ = lp.create_platform_config(ADMIN, lp.DEFAULT_CPMM_CONFIG, "n", "w", "i")
        self.assertEqual(program, lp.LAUNCHLAB)
        self.assertEqual(len(metas), 8)
        self.assertEqual([m[0] for m in metas],
                         [ADMIN, ADMIN, ADMIN, lp.platform_config(ADMIN), lp.DEFAULT_CPMM_CONFIG,
                          lp.SYSTEM, ADMIN, ADMIN])
        # Exactly one signer, and it is the payer.
        self.assertEqual([m[0] for m in metas if m[1]], [ADMIN])

    def test_the_config_account_is_writable_and_unsigned(self):
        _, metas, _ = lp.create_platform_config(ADMIN, lp.DEFAULT_CPMM_CONFIG, "n", "w", "i")
        config = [m for m in metas if m[0] == lp.platform_config(ADMIN)][0]
        self.assertEqual((config[1], config[2]), (False, True))


class TestTheRates(unittest.TestCase):
    def test_the_platform_fee_is_the_house_line(self):
        """2,500 is 0.25% of the quote, which is what every page says the
        protocol takes."""
        self.assertEqual(lp.PLATFORM_FEE_RATE, 2_500)

    def test_lp_goes_entirely_to_the_platform(self):
        """LaunchLab refuses a creator LP slice, and with creators paid
        nothing there is no split to make."""
        self.assertEqual((lp.PLATFORM_SCALE, lp.CREATOR_SCALE, lp.BURN_SCALE), (1_000_000, 0, 0))


if __name__ == "__main__":
    unittest.main()

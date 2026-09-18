//! Charlie Protocol -- the LaunchLab rail.
//!
//! `LAUNCHLAB-RAIL.md` is the design. This crate is build step 1 of its
//! section 8: accounts, the admin and route instructions, the quote-mint
//! guards and the oracle bound, each with tests. The Raydium CPIs (`launch`
//! and the cranks) are steps 2 to 6; their instruction tags are reserved
//! here and refuse with a stated reason until they exist.
//!
//! Same rules as the pump program in `program/`:
//!
//! * no instruction sends to an address its caller supplied: destinations
//!   are derived from seeds or read from this program's own accounts;
//! * native `solana-program`, hand-rolled layouts, no Anchor, so a stranger
//!   can read every byte this program writes;
//! * the only load-bearing assumption is upgrade authority, handed to the
//!   multisig once a graduated mainnet coin has run every crank.

pub mod guards;
pub mod instruction;
pub mod oracle;
pub mod processor;
pub mod raydium;
pub mod state;
pub mod tokenix;

use solana_program::{declare_id, pubkey, pubkey::Pubkey};

// The keypair `cargo-build-sbf` generated at the first build,
// `target/deploy/charlie_launchlab-keypair.json`. A DEVNET id: mainnet gets
// its own keypair, as the pump program's does (BUILD.md sec.10).
#[cfg(feature = "devnet")]
declare_id!("4WgYTmPM9VHyFACSMdw4Bh9jkK9BETjWV5tNrDzuJWgX");
#[cfg(not(feature = "devnet"))]
declare_id!("ENZrfqk89oSQ2fPHPmi6NSMuRDo7F3txEZhi8ZVbCHym");

// -- external programs, read from chain 18 September 2026 --------------------
// Build with `--features devnet` for Raydium's devnet deployments.

#[cfg(not(feature = "devnet"))]
pub const LAUNCHLAB_PROGRAM: Pubkey = pubkey!("LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj");
#[cfg(not(feature = "devnet"))]
pub const CPMM_PROGRAM: Pubkey = pubkey!("CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C");
#[cfg(not(feature = "devnet"))]
pub const LOCK_PROGRAM: Pubkey = pubkey!("LockrWmn6K5twhz3y9w1dQERbmgSaRkfnTeTKbpofwE");

#[cfg(feature = "devnet")]
pub const LAUNCHLAB_PROGRAM: Pubkey = pubkey!("DRay6fNdQ5J82H7xV6uq2aV3mNrUZ1J4PgSKsWgptcm6");
#[cfg(feature = "devnet")]
pub const CPMM_PROGRAM: Pubkey = pubkey!("DRaycpLY18LhpbydsBWbVJtxpNv9oXPgjRSfpF2bWpYb");
#[cfg(feature = "devnet")]
pub const LOCK_PROGRAM: Pubkey = pubkey!("DRay25Usp3YJAi7beckgpGUC7mGJ2cR1AVPxhYfwVCUX");
pub const TOKEN_PROGRAM: Pubkey = pubkey!("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA");
pub const TOKEN_2022_PROGRAM: Pubkey = pubkey!("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb");
pub const WSOL_MINT: Pubkey = pubkey!("So11111111111111111111111111111111111111112");

/// The only address where the runtime destroys lamports. The SOL burn leg
/// closes its wSOL account into it; nothing else is ever paid there.
pub const INCINERATOR: Pubkey = pubkey!("1nc1nerator11111111111111111111111111111111");

// -- the platform's Raydium parameters (LAUNCHLAB-RAIL.md section 1) ---------
// Raydium rates are over 1,000,000; LP scales likewise.

pub const RAYDIUM_RATE_DENOMINATOR: u64 = 1_000_000;
/// Charlie's platform fee: 0.25% of every curve trade. Funds the Charlie leg.
pub const PLATFORM_FEE_RATE: u64 = 2_500;
/// Each coin's creator fee: 0.50%. Funds that coin's dev legs.
pub const CREATOR_FEE_RATE: u64 = 5_000;
/// LP at graduation. LaunchLab refuses any creator slice (`creator_scale`
/// must be 0: `platform_config.rs:58`, found on devnet 18 September 2026),
/// so the whole LP goes to the platform NFT, held by `ll_signer`. Each pool
/// gets its own platform Fee Key, so the LP fees are still attributable per
/// coin, and `crank_lp` splits each pool's claim itself: `LP_CHARLIE_BPS` to
/// the Charlie leg, the rest through that coin's route.
pub const PLATFORM_SCALE: u64 = 1_000_000;
pub const CREATOR_SCALE: u64 = 0;
pub const BURN_SCALE: u64 = 0;
pub const LP_CHARLIE_BPS: u16 = 2_500;

/// A coin's three dev legs fill all of its creator-side fees. On this rail
/// the Charlie leg is a separate Raydium fee stream, not a slice of these,
/// which is why this is 10,000 here and 7,500 on the pump rail.
pub const ROUTE_TOTAL_BPS: u16 = 10_000;

/// Hard ceilings on what `set_quote` accepts, so no admin key can configure
/// a quote whose oracle bound means nothing.
pub const MAX_SLIPPAGE_BPS: u16 = 300;
pub const MAX_STALENESS_S: u32 = 120;

// -- seeds -------------------------------------------------------------------

pub const SEED_PLATFORM: &[u8] = b"ll_platform";
pub const SEED_SIGNER: &[u8] = b"ll_signer";
pub const SEED_COLLECT: &[u8] = b"ll_collect";
pub const SEED_ROUTE: &[u8] = b"ll_route";
pub const SEED_QUOTE: &[u8] = b"quote_cfg";

/// Program-owned. The platform's settings and admin.
pub fn platform_pda() -> (Pubkey, u8) {
    Pubkey::find_program_address(&[SEED_PLATFORM], &id())
}

/// System-owned, never holds data. Raydium's `platform_admin`,
/// `platform_fee_wallet` and `platform_nft_wallet`: it has to be able to sign
/// claims and pay the rent Raydium charges the claimant.
pub fn signer_pda() -> (Pubkey, u8) {
    Pubkey::find_program_address(&[SEED_SIGNER], &id())
}

/// System-owned, never holds data. The coin's LaunchLab creator, CPMM pool
/// creator and creator Fee Key owner.
pub fn collect_pda(mint: &Pubkey) -> (Pubkey, u8) {
    Pubkey::find_program_address(&[SEED_COLLECT, mint.as_ref()], &id())
}

/// Program-owned. The coin's route.
pub fn route_pda(mint: &Pubkey) -> (Pubkey, u8) {
    Pubkey::find_program_address(&[SEED_ROUTE, mint.as_ref()], &id())
}

/// Program-owned. One allowlisted quote asset.
pub fn quote_pda(quote_mint: &Pubkey) -> (Pubkey, u8) {
    Pubkey::find_program_address(&[SEED_QUOTE, quote_mint.as_ref()], &id())
}

#[cfg(not(feature = "no-entrypoint"))]
use processor::process_instruction;
#[cfg(not(feature = "no-entrypoint"))]
solana_program::entrypoint!(process_instruction);

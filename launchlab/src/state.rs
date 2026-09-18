//! Account layouts. Hand-rolled little-endian, first byte a type tag, so an
//! account of one kind can never be read as another even if a derivation
//! check were missed somewhere.

use crate::{MAX_SLIPPAGE_BPS, MAX_STALENESS_S, ROUTE_TOTAL_BPS, TOKEN_2022_PROGRAM, TOKEN_PROGRAM};
use solana_program::{msg, program_error::ProgramError, pubkey::Pubkey};

pub const TAG_PLATFORM: u8 = 1;
pub const TAG_ROUTE: u8 = 2;
pub const TAG_QUOTE: u8 = 3;

struct W<'a> {
    b: &'a mut [u8],
    i: usize,
}
impl<'a> W<'a> {
    fn u8(&mut self, v: u8) {
        self.b[self.i] = v;
        self.i += 1;
    }
    fn u16(&mut self, v: u16) {
        self.b[self.i..self.i + 2].copy_from_slice(&v.to_le_bytes());
        self.i += 2;
    }
    fn u32(&mut self, v: u32) {
        self.b[self.i..self.i + 4].copy_from_slice(&v.to_le_bytes());
        self.i += 4;
    }
    fn u64(&mut self, v: u64) {
        self.b[self.i..self.i + 8].copy_from_slice(&v.to_le_bytes());
        self.i += 8;
    }
    fn key(&mut self, k: &Pubkey) {
        self.b[self.i..self.i + 32].copy_from_slice(k.as_ref());
        self.i += 32;
    }
}

struct R<'a> {
    b: &'a [u8],
    i: usize,
}
impl<'a> R<'a> {
    fn u8(&mut self) -> u8 {
        self.i += 1;
        self.b[self.i - 1]
    }
    fn u16(&mut self) -> u16 {
        self.i += 2;
        u16::from_le_bytes([self.b[self.i - 2], self.b[self.i - 1]])
    }
    fn u32(&mut self) -> u32 {
        self.i += 4;
        u32::from_le_bytes(self.b[self.i - 4..self.i].try_into().unwrap())
    }
    fn u64(&mut self) -> u64 {
        self.i += 8;
        u64::from_le_bytes(self.b[self.i - 8..self.i].try_into().unwrap())
    }
    fn key(&mut self) -> Pubkey {
        self.i += 32;
        Pubkey::new_from_array(self.b[self.i - 32..self.i].try_into().unwrap())
    }
}

fn start<'a>(from: &'a [u8], len: usize, tag: u8) -> Result<R<'a>, ProgramError> {
    if from.len() < len {
        return Err(ProgramError::AccountDataTooSmall);
    }
    if from[0] != tag {
        return Err(ProgramError::InvalidAccountData);
    }
    Ok(R { b: from, i: 1 })
}

fn begin<'a>(into: &'a mut [u8], len: usize, tag: u8) -> Result<W<'a>, ProgramError> {
    if into.len() < len {
        return Err(ProgramError::AccountDataTooSmall);
    }
    let mut w = W { b: into, i: 0 };
    w.u8(tag);
    Ok(w)
}

/// `ll_platform`. The Raydium platform config is written once, by `launch`'s
/// first step (build step 2); until then it is the default key and every
/// instruction that needs it refuses.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Platform {
    pub admin: Pubkey,
    pub raydium_platform_config: Pubkey,
    pub charlie_mint: Pubkey,
    /// The pump rail's `charlie_pool`, where the Charlie leg's SOL goes.
    pub charlie_pool: Pubkey,
    pub paused: bool,
    pub bump: u8,
    pub signer_bump: u8,
}
impl Platform {
    pub const LEN: usize = 1 + 32 * 4 + 1 + 1 + 1;
    pub fn write(&self, into: &mut [u8]) -> Result<(), ProgramError> {
        let mut w = begin(into, Self::LEN, TAG_PLATFORM)?;
        w.key(&self.admin);
        w.key(&self.raydium_platform_config);
        w.key(&self.charlie_mint);
        w.key(&self.charlie_pool);
        w.u8(self.paused as u8);
        w.u8(self.bump);
        w.u8(self.signer_bump);
        Ok(())
    }
    pub fn read(from: &[u8]) -> Result<Self, ProgramError> {
        let mut r = start(from, Self::LEN, TAG_PLATFORM)?;
        Ok(Self {
            admin: r.key(),
            raydium_platform_config: r.key(),
            charlie_mint: r.key(),
            charlie_pool: r.key(),
            paused: r.u8() == 1,
            bump: r.u8(),
            signer_bump: r.u8(),
        })
    }
}

/// `ll_route(mint)`. The dev's split of every creator-side fee the coin earns.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Route {
    pub mint: Pubkey,
    pub quote_mint: Pubkey,
    /// The coin's admin: the only key `set_route` accepts.
    pub admin: Pubkey,
    pub sol_burn_bps: u16,
    pub own_burn_bps: u16,
    pub ops_bps: u16,
    pub ops_address: Pubkey,
    pub launchlab_pool: Pubkey,
    /// Default until graduation.
    pub cpmm_pool: Pubkey,
    /// Lifetime totals, raw units. Written only by the cranks.
    pub coin_burned: u64,
    pub lamports_burned: u64,
    pub ops_paid: u64,
    pub bump: u8,
    pub collect_bump: u8,
}
impl Route {
    pub const LEN: usize = 1 + 32 * 3 + 2 * 3 + 32 * 3 + 8 * 3 + 1 + 1;

    /// The invariant, checked on every write: the three legs fill exactly
    /// the coin's creator-side fees, and a nonzero ops leg has somewhere to go.
    pub fn validate(&self) -> Result<(), ProgramError> {
        let sum = self.sol_burn_bps as u32 + self.own_burn_bps as u32 + self.ops_bps as u32;
        if sum != ROUTE_TOTAL_BPS as u32 {
            msg!("route: the three legs must sum to 10000");
            return Err(ProgramError::InvalidInstructionData);
        }
        if self.ops_bps > 0 && self.ops_address == Pubkey::default() {
            msg!("route: an ops share needs an ops address");
            return Err(ProgramError::InvalidArgument);
        }
        Ok(())
    }

    pub fn write(&self, into: &mut [u8]) -> Result<(), ProgramError> {
        self.validate()?;
        let mut w = begin(into, Self::LEN, TAG_ROUTE)?;
        w.key(&self.mint);
        w.key(&self.quote_mint);
        w.key(&self.admin);
        w.u16(self.sol_burn_bps);
        w.u16(self.own_burn_bps);
        w.u16(self.ops_bps);
        w.key(&self.ops_address);
        w.key(&self.launchlab_pool);
        w.key(&self.cpmm_pool);
        w.u64(self.coin_burned);
        w.u64(self.lamports_burned);
        w.u64(self.ops_paid);
        w.u8(self.bump);
        w.u8(self.collect_bump);
        Ok(())
    }
    pub fn read(from: &[u8]) -> Result<Self, ProgramError> {
        let mut r = start(from, Self::LEN, TAG_ROUTE)?;
        Ok(Self {
            mint: r.key(),
            quote_mint: r.key(),
            admin: r.key(),
            sol_burn_bps: r.u16(),
            own_burn_bps: r.u16(),
            ops_bps: r.u16(),
            ops_address: r.key(),
            launchlab_pool: r.key(),
            cpmm_pool: r.key(),
            coin_burned: r.u64(),
            lamports_burned: r.u64(),
            ops_paid: r.u64(),
            bump: r.u8(),
            collect_bump: r.u8(),
        })
    }
}

/// `quote_cfg(quote)`. One allowlisted quote asset and the bound every swap
/// out of it must meet.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct QuoteCfg {
    pub quote_mint: Pubkey,
    pub token_program: Pubkey,
    pub decimals: u8,
    pub enabled: bool,
    /// Pull-oracle price account for the quote asset's underlying, in USD.
    /// Default key for a SOL quote, which needs no swap.
    pub oracle: Pubkey,
    pub max_slippage_bps: u16,
    pub max_staleness_s: u32,
    /// Smallest claim, raw units, a crank may route.
    pub min_crank: u64,
    pub bump: u8,
}
impl QuoteCfg {
    pub const LEN: usize = 1 + 32 * 2 + 1 + 1 + 32 + 2 + 4 + 8 + 1;

    pub fn is_sol(&self) -> bool {
        self.quote_mint == crate::WSOL_MINT
    }

    pub fn validate(&self) -> Result<(), ProgramError> {
        if self.token_program != TOKEN_PROGRAM && self.token_program != TOKEN_2022_PROGRAM {
            return Err(ProgramError::IncorrectProgramId);
        }
        if self.max_slippage_bps > MAX_SLIPPAGE_BPS || self.max_staleness_s > MAX_STALENESS_S {
            msg!("quote_cfg: slippage or staleness above the program's ceiling");
            return Err(ProgramError::InvalidArgument);
        }
        // The rule the design states: no enabled non-SOL quote without an
        // oracle, because without one a permissionless crank can pick the price.
        if self.enabled && !self.is_sol() && self.oracle == Pubkey::default() {
            msg!("quote_cfg: a non-SOL quote cannot be enabled without an oracle");
            return Err(ProgramError::InvalidArgument);
        }
        Ok(())
    }

    pub fn write(&self, into: &mut [u8]) -> Result<(), ProgramError> {
        self.validate()?;
        let mut w = begin(into, Self::LEN, TAG_QUOTE)?;
        w.key(&self.quote_mint);
        w.key(&self.token_program);
        w.u8(self.decimals);
        w.u8(self.enabled as u8);
        w.key(&self.oracle);
        w.u16(self.max_slippage_bps);
        w.u32(self.max_staleness_s);
        w.u64(self.min_crank);
        w.u8(self.bump);
        Ok(())
    }
    pub fn read(from: &[u8]) -> Result<Self, ProgramError> {
        let mut r = start(from, Self::LEN, TAG_QUOTE)?;
        Ok(Self {
            quote_mint: r.key(),
            token_program: r.key(),
            decimals: r.u8(),
            enabled: r.u8() == 1,
            oracle: r.key(),
            max_slippage_bps: r.u16(),
            max_staleness_s: r.u32(),
            min_crank: r.u64(),
            bump: r.u8(),
        })
    }
}

/// The dev legs out of `q` quote units. Integer division; the remainder (at
/// most two raw units) is swept to no leg, the same rule as the pump
/// program's `split_legs`: it stays in `ll_collect`'s token account and is
/// routed with the next claim, because cranks route the account's whole
/// balance. No leg is ever paid a fraction of a basis point over its share.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Legs {
    pub sol_burn: u64,
    pub own_burn: u64,
    pub ops: u64,
    pub remainder: u64,
}

pub fn split_legs(q: u64, route: &Route) -> Legs {
    let share = |bps: u16| (q as u128 * bps as u128 / ROUTE_TOTAL_BPS as u128) as u64;
    let sol_burn = share(route.sol_burn_bps);
    let own_burn = share(route.own_burn_bps);
    let ops = share(route.ops_bps);
    Legs { sol_burn, own_burn, ops, remainder: q - sol_burn - own_burn - ops }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn k(n: u8) -> Pubkey {
        Pubkey::new_from_array([n; 32])
    }

    fn route() -> Route {
        Route {
            mint: k(1),
            quote_mint: k(2),
            admin: k(3),
            sol_burn_bps: 4_000,
            own_burn_bps: 3_500,
            ops_bps: 2_500,
            ops_address: k(4),
            launchlab_pool: k(5),
            cpmm_pool: Pubkey::default(),
            coin_burned: 7,
            lamports_burned: 8,
            ops_paid: 9,
            bump: 254,
            collect_bump: 253,
        }
    }

    #[test]
    fn route_round_trips() {
        let r = route();
        let mut buf = vec![0u8; Route::LEN];
        r.write(&mut buf).unwrap();
        assert_eq!(Route::read(&buf).unwrap(), r);
        assert_eq!(buf[0], TAG_ROUTE);
    }

    #[test]
    fn route_must_sum_to_ten_thousand() {
        let mut r = route();
        r.ops_bps = 2_499;
        assert!(r.validate().is_err());
        let mut buf = vec![0u8; Route::LEN];
        assert!(r.write(&mut buf).is_err(), "write re-checks the invariant");
        r.ops_bps = 2_501;
        assert!(r.validate().is_err());
    }

    #[test]
    fn route_ops_share_needs_an_address() {
        let mut r = route();
        r.ops_address = Pubkey::default();
        assert!(r.validate().is_err());
        r.sol_burn_bps = 6_500;
        r.ops_bps = 0;
        assert!(r.validate().is_ok(), "no ops share, no address needed");
    }

    #[test]
    fn all_burn_is_a_valid_route() {
        let mut r = route();
        r.sol_burn_bps = 0;
        r.own_burn_bps = 10_000;
        r.ops_bps = 0;
        assert!(r.validate().is_ok());
    }

    #[test]
    fn wrong_tag_is_refused() {
        let r = route();
        let mut buf = vec![0u8; Route::LEN];
        r.write(&mut buf).unwrap();
        buf[0] = TAG_QUOTE;
        assert!(Route::read(&buf).is_err());
        assert!(Route::read(&buf[..Route::LEN - 1]).is_err());
    }

    #[test]
    fn platform_round_trips() {
        let p = Platform {
            admin: k(9),
            raydium_platform_config: Pubkey::default(),
            charlie_mint: k(10),
            charlie_pool: k(11),
            paused: true,
            bump: 250,
            signer_bump: 251,
        };
        let mut buf = vec![0u8; Platform::LEN];
        p.write(&mut buf).unwrap();
        assert_eq!(Platform::read(&buf).unwrap(), p);
    }

    fn quote() -> QuoteCfg {
        QuoteCfg {
            quote_mint: k(20),
            token_program: TOKEN_2022_PROGRAM,
            decimals: 8,
            enabled: true,
            oracle: k(21),
            max_slippage_bps: 100,
            max_staleness_s: 60,
            min_crank: 1_000_000,
            bump: 249,
        }
    }

    #[test]
    fn quote_round_trips() {
        let q = quote();
        let mut buf = vec![0u8; QuoteCfg::LEN];
        q.write(&mut buf).unwrap();
        assert_eq!(QuoteCfg::read(&buf).unwrap(), q);
    }

    #[test]
    fn quote_ceilings_hold() {
        let mut q = quote();
        q.max_slippage_bps = MAX_SLIPPAGE_BPS + 1;
        assert!(q.validate().is_err());
        let mut q = quote();
        q.max_staleness_s = MAX_STALENESS_S + 1;
        assert!(q.validate().is_err());
        let mut q = quote();
        q.token_program = k(99);
        assert!(q.validate().is_err());
    }

    #[test]
    fn no_enabled_stock_quote_without_an_oracle() {
        let mut q = quote();
        q.oracle = Pubkey::default();
        assert!(q.validate().is_err());
        q.enabled = false;
        assert!(q.validate().is_ok(), "may be staged disabled");
        let mut sol = quote();
        sol.quote_mint = crate::WSOL_MINT;
        sol.token_program = TOKEN_PROGRAM;
        sol.oracle = Pubkey::default();
        assert!(sol.validate().is_ok(), "a SOL quote never swaps");
    }

    #[test]
    fn legs_never_exceed_the_claim() {
        let r = route();
        for q in [0u64, 1, 3, 9_999, 10_001, 123_456_789, u64::MAX] {
            let l = split_legs(q, &r);
            assert_eq!(l.sol_burn as u128 + l.own_burn as u128 + l.ops as u128 + l.remainder as u128, q as u128);
            assert!(l.remainder < 3, "at most one unit per leg is left behind");
        }
        let l = split_legs(1_000_000, &r);
        assert_eq!((l.sol_burn, l.own_burn, l.ops, l.remainder), (400_000, 350_000, 250_000, 0));
    }
}

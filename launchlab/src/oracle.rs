//! The oracle bound on every swap out of a quote asset (LAUNCHLAB-RAIL.md
//! section 5). A permissionless caller picks the route; this picks the
//! floor, so the caller cannot pick the price.
//!
//! Pure integer arithmetic over prices already read from the oracle account.
//! Reading the oracle account itself is build step 5, once the feed for
//! each allowlisted stock is confirmed to exist.

use solana_program::{msg, program_error::ProgramError};

/// One oracle reading: `price x 10^expo` USD, +/- `conf x 10^expo`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Price {
    pub price: i64,
    pub conf: u64,
    pub expo: i32,
    pub publish_time: i64,
}

impl Price {
    /// Refuses a reading older than `max_staleness_s` at `now`, a
    /// non-positive price, or a confidence wider than the price itself.
    /// Staleness is what stops swaps while the stock market is closed: the
    /// feed stops publishing and every crank refuses until it resumes.
    pub fn fresh(&self, now: i64, max_staleness_s: u32) -> Result<(), ProgramError> {
        if self.price <= 0 || self.conf >= self.price as u64 {
            msg!("oracle: unusable price");
            return Err(ProgramError::InvalidAccountData);
        }
        if now.saturating_sub(self.publish_time) > max_staleness_s as i64 {
            msg!("oracle: stale");
            return Err(ProgramError::InvalidAccountData);
        }
        Ok(())
    }
}

/// Fixed-point scale for the mint's scaled-UI multiplier.
pub const MULT_SCALE: u128 = 1_000_000_000;

/// The multiplier as fixed point, rounded DOWN, so the bound it produces is
/// never higher than the true value would give.
pub fn multiplier_fixed(m: f64) -> Result<u128, ProgramError> {
    if !(m.is_finite() && m > 0.0 && m < 1_000.0) {
        return Err(ProgramError::InvalidAccountData);
    }
    Ok((m * MULT_SCALE as f64) as u128)
}

fn pow10(e: u32) -> Result<u128, ProgramError> {
    10u128.checked_pow(e).ok_or(ProgramError::ArithmeticOverflow)
}

/// The least lamports a swap of `q_raw` quote units must return.
///
/// value = q_raw / 10^decimals x multiplier x quote price, in SOL at the SOL
/// price, less `max_slippage_bps`. Conservative on every input: the quote
/// price is taken at its low edge (price - conf), SOL at its high edge
/// (price + conf), and every division rounds down.
pub fn min_lamports_out(
    q_raw: u64,
    q_decimals: u8,
    multiplier: u128,
    quote: &Price,
    sol: &Price,
    max_slippage_bps: u16,
) -> Result<u64, ProgramError> {
    let q_low = (quote.price as u128)
        .checked_sub(quote.conf as u128)
        .ok_or(ProgramError::InvalidAccountData)?;
    let sol_high = (sol.price as u128)
        .checked_add(sol.conf as u128)
        .ok_or(ProgramError::ArithmeticOverflow)?;
    if q_low == 0 || sol_high == 0 {
        return Err(ProgramError::InvalidAccountData);
    }

    // lamports = q_raw x 10^(9 - decimals + qexpo - solexpo) x mult x q_low / sol_high
    let e: i64 = 9 - q_decimals as i64 + quote.expo as i64 - sol.expo as i64;
    let mut num = (q_raw as u128)
        .checked_mul(multiplier)
        .and_then(|v| v.checked_mul(q_low))
        .ok_or(ProgramError::ArithmeticOverflow)?;
    let mut den = sol_high.checked_mul(MULT_SCALE).ok_or(ProgramError::ArithmeticOverflow)?;
    if e >= 0 {
        num = num.checked_mul(pow10(e as u32)?).ok_or(ProgramError::ArithmeticOverflow)?;
    } else {
        den = den.checked_mul(pow10((-e) as u32)?).ok_or(ProgramError::ArithmeticOverflow)?;
    }
    let fair = num / den;
    let floor = fair * (10_000 - max_slippage_bps as u128) / 10_000;
    u64::try_from(floor).map_err(|_| ProgramError::ArithmeticOverflow)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn p(price: i64, conf: u64, expo: i32) -> Price {
        Price { price, conf, expo, publish_time: 1_000 }
    }

    #[test]
    fn one_unit_at_even_prices() {
        // 1.00000000 of an 8-decimal token at $150, SOL at $150, multiplier 1,
        // no slippage: exactly 1 SOL.
        let out = min_lamports_out(100_000_000, 8, MULT_SCALE, &p(15_000_000_000, 0, -8), &p(15_000_000_000, 0, -8), 0)
            .unwrap();
        assert_eq!(out, 1_000_000_000);
    }

    #[test]
    fn spyx_sized_example() {
        // 0.15 SPYx (8 decimals) at $660 x multiplier 1.005714560286254, SOL
        // at $95: 0.15 x 660 x 1.0057... / 95 = 1.048 SOL fair; 100 bps off.
        let m = multiplier_fixed(1.005714560286254).unwrap();
        let out = min_lamports_out(15_000_000, 8, m, &p(66_000_000_000, 0, -8), &p(9_500_000_000, 0, -8), 100)
            .unwrap();
        let fair = 0.15 * 660.0 * 1.005714560286254 / 95.0;
        let expect = (fair * 0.99 * 1e9) as u64;
        assert!(out <= expect && expect - out < 10, "{out} vs {expect}");
    }

    #[test]
    fn confidence_only_lowers_the_floor() {
        let tight = min_lamports_out(100_000_000, 8, MULT_SCALE, &p(15_000_000_000, 0, -8), &p(15_000_000_000, 0, -8), 50)
            .unwrap();
        let wide = min_lamports_out(
            100_000_000,
            8,
            MULT_SCALE,
            &p(15_000_000_000, 150_000_000, -8),
            &p(15_000_000_000, 150_000_000, -8),
            50,
        )
        .unwrap();
        assert!(wide < tight);
    }

    #[test]
    fn different_exponents_agree() {
        let a = min_lamports_out(100_000_000, 8, MULT_SCALE, &p(15_000_000_000, 0, -8), &p(15_000, 0, -2), 0).unwrap();
        let b = min_lamports_out(100_000_000, 8, MULT_SCALE, &p(150_000, 0, -3), &p(15_000_000_000, 0, -8), 0).unwrap();
        assert_eq!(a, 1_000_000_000);
        assert_eq!(b, 1_000_000_000);
    }

    #[test]
    fn freshness() {
        let x = p(15_000_000_000, 1, -8);
        assert!(x.fresh(1_060, 60).is_ok());
        assert!(x.fresh(1_061, 60).is_err());
        assert!(p(0, 0, -8).fresh(1_000, 60).is_err());
        assert!(p(100, 100, -8).fresh(1_000, 60).is_err(), "conf as wide as price");
    }

    #[test]
    fn multiplier_rounds_down_and_rejects_nonsense() {
        assert_eq!(multiplier_fixed(1.0).unwrap(), MULT_SCALE);
        assert!(multiplier_fixed(1.0000000019).unwrap() <= 1_000_000_001);
        for bad in [0.0, -1.0, f64::NAN, f64::INFINITY, 5_000.0] {
            assert!(multiplier_fixed(bad).is_err());
        }
    }

    #[test]
    fn overflow_is_an_error_not_a_wrap() {
        assert!(min_lamports_out(u64::MAX, 0, MULT_SCALE * 999, &p(i64::MAX / 2, 0, 10), &p(1, 0, -10), 0).is_err());
    }
}

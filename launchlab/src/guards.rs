//! The quote-mint guards every crank runs before a transfer
//! (LAUNCHLAB-RAIL.md section 5), read straight from the mint's bytes.
//!
//! Token-2022 mint layout: the 82-byte base mint, zero padding to 165, an
//! account-type byte at 165 (1 = mint), then TLV entries from 166: u16 type,
//! u16 length, value. Checked against SPYx's live mint account in
//! `tests/fixtures/spyx_mint_slot448127246.bin`.

use solana_program::{msg, program_error::ProgramError, pubkey::Pubkey};

pub const BASE_MINT_LEN: usize = 82;
pub const ACCOUNT_TYPE_OFFSET: usize = 165;
pub const TLV_START: usize = 166;
pub const ACCOUNT_TYPE_MINT: u8 = 1;

// spl-token-2022 `ExtensionType` values.
pub const EXT_TRANSFER_HOOK: u16 = 14;
pub const EXT_SCALED_UI_AMOUNT: u16 = 25;
pub const EXT_PAUSABLE: u16 = 26;

/// What the guards need out of a quote mint.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct QuoteMintState {
    pub decimals: u8,
    /// `None` when the extension is absent; `Some(default)` when present and
    /// unset, which is xStocks today.
    pub hook_program: Option<Pubkey>,
    pub paused: bool,
    /// `(multiplier, effective_at, new_multiplier)`.
    pub scaled: Option<(f64, i64, f64)>,
}

impl QuoteMintState {
    /// The multiplier in force at `now`. 1.0 without the extension.
    pub fn multiplier_at(&self, now: i64) -> f64 {
        match self.scaled {
            None => 1.0,
            Some((m, at, new_m)) => {
                if now >= at {
                    new_m
                } else {
                    m
                }
            }
        }
    }
}

fn get<const N: usize>(b: &[u8], at: usize) -> Result<[u8; N], ProgramError> {
    b.get(at..at + N)
        .and_then(|s| s.try_into().ok())
        .ok_or(ProgramError::InvalidAccountData)
}

/// Reads a mint account. A legacy SPL mint (exactly 82 bytes) has no
/// extensions and passes every guard.
pub fn read_quote_mint(data: &[u8]) -> Result<QuoteMintState, ProgramError> {
    if data.len() < BASE_MINT_LEN {
        return Err(ProgramError::InvalidAccountData);
    }
    let decimals = data[44];
    let mut st = QuoteMintState { decimals, hook_program: None, paused: false, scaled: None };
    if data.len() == BASE_MINT_LEN {
        return Ok(st);
    }
    if data.len() <= TLV_START || data[ACCOUNT_TYPE_OFFSET] != ACCOUNT_TYPE_MINT {
        return Err(ProgramError::InvalidAccountData);
    }
    let mut i = TLV_START;
    while i + 4 <= data.len() {
        let ty = u16::from_le_bytes(get::<2>(data, i)?);
        let len = u16::from_le_bytes(get::<2>(data, i + 2)?) as usize;
        if ty == 0 {
            break;
        }
        let v = data.get(i + 4..i + 4 + len).ok_or(ProgramError::InvalidAccountData)?;
        match ty {
            EXT_TRANSFER_HOOK => {
                // authority (32) | program_id (32)
                let p: [u8; 32] = get::<32>(v, 32)?;
                st.hook_program = Some(Pubkey::new_from_array(p));
            }
            EXT_PAUSABLE => {
                // authority (32) | paused (1)
                st.paused = *v.get(32).ok_or(ProgramError::InvalidAccountData)? != 0;
            }
            EXT_SCALED_UI_AMOUNT => {
                // authority (32) | multiplier f64 | new_multiplier_effective_timestamp i64 | new_multiplier f64
                let m = f64::from_le_bytes(get::<8>(v, 32)?);
                let at = i64::from_le_bytes(get::<8>(v, 40)?);
                let nm = f64::from_le_bytes(get::<8>(v, 48)?);
                st.scaled = Some((m, at, nm));
            }
            _ => {}
        }
        i += 4 + len;
    }
    Ok(st)
}

/// The guard itself. Refuses a mint whose transfer hook points anywhere
/// (Raydium passes no hook accounts, so a live hook breaks the pool) or
/// that is paused. Either one aborts the crank before anything moves, and
/// the fees wait in Raydium's vaults.
pub fn check_quote_mint(data: &[u8]) -> Result<QuoteMintState, ProgramError> {
    let st = read_quote_mint(data)?;
    if let Some(p) = st.hook_program {
        if p != Pubkey::default() {
            msg!("guard: quote mint has a live transfer hook");
            return Err(ProgramError::InvalidAccountData);
        }
    }
    if st.paused {
        msg!("guard: quote mint is paused");
        return Err(ProgramError::InvalidAccountData);
    }
    Ok(st)
}

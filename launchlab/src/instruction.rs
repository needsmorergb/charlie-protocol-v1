//! Instruction data: a one-byte tag, then fixed little-endian fields.
//!
//! Tags 10 to 16 are reserved for the Raydium instructions of build steps 2
//! to 6 and 9. They parse, so clients can be written against them now, and
//! the processor refuses them with the step that builds them.

use solana_program::{program_error::ProgramError, pubkey::Pubkey};

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Instruction {
    /// 0. Accounts: `[s, w] payer = program upgrade authority`, `[w] ll_platform`,
    /// `[] program data`, `[] system`.
    InitPlatform { admin: Pubkey, charlie_mint: Pubkey, charlie_pool: Pubkey },
    /// 1. Accounts: `[s, w] admin`, `[] ll_platform`, `[w] quote_cfg`, `[] quote mint`, `[] system`.
    /// Decimals and token program are read from the mint, never taken as args.
    SetQuote { enabled: bool, oracle: Pubkey, max_slippage_bps: u16, max_staleness_s: u32, min_crank: u64 },
    /// 2. Accounts: `[s] coin admin`, `[w] ll_route`.
    SetRoute { sol_burn_bps: u16, own_burn_bps: u16, ops_bps: u16, ops_address: Pubkey },
    /// 3. Accounts: `[s] admin`, `[w] ll_platform`.
    SetPaused { paused: bool },
    /// 4. Accounts: `[s] admin`, `[w] ll_platform`. How the admin moves to the multisig.
    SetAdmin { new_admin: Pubkey },
    /// 5. Once. Creates Charlie's Raydium platform config with `ll_signer` as
    /// its admin and every wallet, funding `ll_signer` with `fund_lamports`
    /// first so it can pay the config's rent. Accounts: `[s, w] admin`,
    /// `[w] ll_platform`, `[w] ll_signer`, `[w] raydium platform config`,
    /// `[] cpswap config`, `[] system`, `[] LaunchLab program`.
    CreateRaydiumPlatform { fund_lamports: u64 },

    /// 10. Launch a coin. Accounts in `processor::launch`.
    Launch(LaunchArgs),
    /// 11. Claim the curve creator fee and route it. Accounts in `processor::crank_curve`.
    CrankCurve,
    /// 12. After graduation: collect the CPMM pool's creator fee into
    /// `ll_collect` and route it. Accounts in `processor::crank_amm_creator`.
    CrankAmmCreator,
    /// 13. After graduation: `ll_signer` claims the pool's LP fees with its
    /// Fee Key; 25% of the quote side to the Charlie leg, the rest routed.
    /// `lp_fee_amount` is the lock program's argument, computed off-chain
    /// (the program is closed-source); what is routed is what arrives.
    CrankLp { lp_fee_amount: u64 },
    /// 14. Build step 6.
    CrankPlatform,
    /// 15. Build step 9 (timelocked Raydium platform-config changes).
    ProposePlatformChange,
    /// 16. Build step 9.
    ApplyPlatformChange,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LaunchArgs {
    pub decimals: u8,
    pub name: String,
    pub symbol: String,
    pub uri: String,
    pub supply: u64,
    pub total_base_sell: u64,
    pub total_quote_fund_raising: u64,
    pub sol_burn_bps: u16,
    pub own_burn_bps: u16,
    pub ops_bps: u16,
    pub ops_address: Pubkey,
    /// Lamports the dev moves into `ll_collect` so it can pay the rent
    /// Raydium charges the claimant.
    pub fund_collect: u64,
}

pub const MAX_NAME: usize = 32;
pub const MAX_SYMBOL: usize = 10;
pub const MAX_URI: usize = 200;

struct Cur<'a> {
    d: &'a [u8],
    i: usize,
}
impl<'a> Cur<'a> {
    fn take(&mut self, n: usize) -> Result<&'a [u8], ProgramError> {
        let s = self.d.get(self.i..self.i + n).ok_or(ProgramError::InvalidInstructionData)?;
        self.i += n;
        Ok(s)
    }
    fn u8(&mut self) -> Result<u8, ProgramError> {
        Ok(self.take(1)?[0])
    }
    fn boolean(&mut self) -> Result<bool, ProgramError> {
        match self.u8()? {
            0 => Ok(false),
            1 => Ok(true),
            _ => Err(ProgramError::InvalidInstructionData),
        }
    }
    fn u16(&mut self) -> Result<u16, ProgramError> {
        Ok(u16::from_le_bytes(self.take(2)?.try_into().unwrap()))
    }
    fn u32(&mut self) -> Result<u32, ProgramError> {
        Ok(u32::from_le_bytes(self.take(4)?.try_into().unwrap()))
    }
    fn u64(&mut self) -> Result<u64, ProgramError> {
        Ok(u64::from_le_bytes(self.take(8)?.try_into().unwrap()))
    }
    fn key(&mut self) -> Result<Pubkey, ProgramError> {
        Ok(Pubkey::new_from_array(self.take(32)?.try_into().unwrap()))
    }
    fn string(&mut self, max: usize) -> Result<String, ProgramError> {
        let n = self.u32()? as usize;
        if n > max {
            return Err(ProgramError::InvalidInstructionData);
        }
        String::from_utf8(self.take(n)?.to_vec()).map_err(|_| ProgramError::InvalidInstructionData)
    }
    fn end(&self) -> Result<(), ProgramError> {
        if self.i == self.d.len() {
            Ok(())
        } else {
            Err(ProgramError::InvalidInstructionData)
        }
    }
}

impl Instruction {
    pub fn unpack(data: &[u8]) -> Result<Self, ProgramError> {
        let mut c = Cur { d: data, i: 0 };
        let ix = match c.u8()? {
            0 => Self::InitPlatform { admin: c.key()?, charlie_mint: c.key()?, charlie_pool: c.key()? },
            1 => Self::SetQuote {
                enabled: c.boolean()?,
                oracle: c.key()?,
                max_slippage_bps: c.u16()?,
                max_staleness_s: c.u32()?,
                min_crank: c.u64()?,
            },
            2 => Self::SetRoute {
                sol_burn_bps: c.u16()?,
                own_burn_bps: c.u16()?,
                ops_bps: c.u16()?,
                ops_address: c.key()?,
            },
            3 => Self::SetPaused { paused: c.boolean()? },
            4 => Self::SetAdmin { new_admin: c.key()? },
            5 => Self::CreateRaydiumPlatform { fund_lamports: c.u64()? },
            10 => Self::Launch(LaunchArgs {
                decimals: c.u8()?,
                name: c.string(MAX_NAME)?,
                symbol: c.string(MAX_SYMBOL)?,
                uri: c.string(MAX_URI)?,
                supply: c.u64()?,
                total_base_sell: c.u64()?,
                total_quote_fund_raising: c.u64()?,
                sol_burn_bps: c.u16()?,
                own_burn_bps: c.u16()?,
                ops_bps: c.u16()?,
                ops_address: c.key()?,
                fund_collect: c.u64()?,
            }),
            11 => Self::CrankCurve,
            12 => Self::CrankAmmCreator,
            13 => Self::CrankLp { lp_fee_amount: c.u64()? },
            14 => Self::CrankPlatform,
            15 => Self::ProposePlatformChange,
            16 => Self::ApplyPlatformChange,
            _ => return Err(ProgramError::InvalidInstructionData),
        };
        // Reserved tags carry their own data once built; until then, none.
        c.end()?;
        Ok(ix)
    }

    pub fn pack(&self) -> Vec<u8> {
        let mut v = Vec::new();
        match self {
            Self::InitPlatform { admin, charlie_mint, charlie_pool } => {
                v.push(0);
                v.extend_from_slice(admin.as_ref());
                v.extend_from_slice(charlie_mint.as_ref());
                v.extend_from_slice(charlie_pool.as_ref());
            }
            Self::SetQuote { enabled, oracle, max_slippage_bps, max_staleness_s, min_crank } => {
                v.push(1);
                v.push(*enabled as u8);
                v.extend_from_slice(oracle.as_ref());
                v.extend_from_slice(&max_slippage_bps.to_le_bytes());
                v.extend_from_slice(&max_staleness_s.to_le_bytes());
                v.extend_from_slice(&min_crank.to_le_bytes());
            }
            Self::SetRoute { sol_burn_bps, own_burn_bps, ops_bps, ops_address } => {
                v.push(2);
                v.extend_from_slice(&sol_burn_bps.to_le_bytes());
                v.extend_from_slice(&own_burn_bps.to_le_bytes());
                v.extend_from_slice(&ops_bps.to_le_bytes());
                v.extend_from_slice(ops_address.as_ref());
            }
            Self::SetPaused { paused } => {
                v.push(3);
                v.push(*paused as u8);
            }
            Self::SetAdmin { new_admin } => {
                v.push(4);
                v.extend_from_slice(new_admin.as_ref());
            }
            Self::CreateRaydiumPlatform { fund_lamports } => {
                v.push(5);
                v.extend_from_slice(&fund_lamports.to_le_bytes());
            }
            Self::Launch(a) => {
                v.push(10);
                v.push(a.decimals);
                for s in [&a.name, &a.symbol, &a.uri] {
                    v.extend_from_slice(&(s.len() as u32).to_le_bytes());
                    v.extend_from_slice(s.as_bytes());
                }
                v.extend_from_slice(&a.supply.to_le_bytes());
                v.extend_from_slice(&a.total_base_sell.to_le_bytes());
                v.extend_from_slice(&a.total_quote_fund_raising.to_le_bytes());
                v.extend_from_slice(&a.sol_burn_bps.to_le_bytes());
                v.extend_from_slice(&a.own_burn_bps.to_le_bytes());
                v.extend_from_slice(&a.ops_bps.to_le_bytes());
                v.extend_from_slice(a.ops_address.as_ref());
                v.extend_from_slice(&a.fund_collect.to_le_bytes());
            }
            Self::CrankCurve => v.push(11),
            Self::CrankAmmCreator => v.push(12),
            Self::CrankLp { lp_fee_amount } => {
                v.push(13);
                v.extend_from_slice(&lp_fee_amount.to_le_bytes());
            }
            Self::CrankPlatform => v.push(14),
            Self::ProposePlatformChange => v.push(15),
            Self::ApplyPlatformChange => v.push(16),
        }
        v
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn k(n: u8) -> Pubkey {
        Pubkey::new_from_array([n; 32])
    }

    #[test]
    fn every_instruction_round_trips() {
        let all = vec![
            Instruction::InitPlatform { admin: k(1), charlie_mint: k(2), charlie_pool: k(3) },
            Instruction::SetQuote { enabled: true, oracle: k(4), max_slippage_bps: 100, max_staleness_s: 60, min_crank: 5 },
            Instruction::SetRoute { sol_burn_bps: 1, own_burn_bps: 2, ops_bps: 3, ops_address: k(6) },
            Instruction::SetPaused { paused: true },
            Instruction::SetAdmin { new_admin: k(7) },
            Instruction::CreateRaydiumPlatform { fund_lamports: 9 },
            Instruction::Launch(LaunchArgs {
                decimals: 6,
                name: "Charlie Test".into(),
                symbol: "CTEST".into(),
                uri: "https://x".into(),
                supply: 1_000_000_000_000_000,
                total_base_sell: 793_100_000_000_000,
                total_quote_fund_raising: 500_000_000,
                sol_burn_bps: 4_000,
                own_burn_bps: 4_000,
                ops_bps: 2_000,
                ops_address: k(8),
                fund_collect: 10_000_000,
            }),
            Instruction::CrankCurve,
            Instruction::CrankAmmCreator,
            Instruction::CrankLp { lp_fee_amount: 77 },
            Instruction::CrankPlatform,
            Instruction::ProposePlatformChange,
            Instruction::ApplyPlatformChange,
        ];
        for ix in all {
            assert_eq!(Instruction::unpack(&ix.pack()).unwrap(), ix);
        }
    }

    #[test]
    fn trailing_bytes_short_data_and_bad_bools_are_refused() {
        let mut d = Instruction::SetPaused { paused: false }.pack();
        d.push(0);
        assert!(Instruction::unpack(&d).is_err());
        assert!(Instruction::unpack(&[2, 1, 0]).is_err());
        assert!(Instruction::unpack(&[3, 2]).is_err());
        assert!(Instruction::unpack(&[6]).is_err());
        // an over-long name is refused before it is read
        let mut d = vec![10, 6];
        d.extend_from_slice(&(MAX_NAME as u32 + 1).to_le_bytes());
        assert!(Instruction::unpack(&d).is_err());
        assert!(Instruction::unpack(&[]).is_err());
    }
}

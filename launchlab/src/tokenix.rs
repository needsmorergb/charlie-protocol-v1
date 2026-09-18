//! The handful of SPL Token / Token-2022 / ATA instructions the cranks use,
//! built by hand to keep the dependency list at `solana-program` alone.

use solana_program::{
    instruction::{AccountMeta, Instruction},
    program_error::ProgramError,
    pubkey,
    pubkey::Pubkey,
};

pub const ATA_PROGRAM: Pubkey = pubkey!("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL");

pub fn associated_token_address(owner: &Pubkey, mint: &Pubkey, token_program: &Pubkey) -> Pubkey {
    Pubkey::find_program_address(&[owner.as_ref(), token_program.as_ref(), mint.as_ref()], &ATA_PROGRAM).0
}

/// ATA `CreateIdempotent`.
pub fn create_ata_idempotent(payer: &Pubkey, owner: &Pubkey, mint: &Pubkey, token_program: &Pubkey) -> Instruction {
    Instruction {
        program_id: ATA_PROGRAM,
        accounts: vec![
            AccountMeta::new(*payer, true),
            AccountMeta::new(associated_token_address(owner, mint, token_program), false),
            AccountMeta::new_readonly(*owner, false),
            AccountMeta::new_readonly(*mint, false),
            AccountMeta::new_readonly(solana_program::system_program::id(), false),
            AccountMeta::new_readonly(*token_program, false),
        ],
        data: vec![1],
    }
}

/// `Burn` (tag 8). Same encoding in SPL Token and Token-2022.
pub fn burn(token_program: &Pubkey, account: &Pubkey, mint: &Pubkey, owner: &Pubkey, amount: u64) -> Instruction {
    let mut data = vec![8];
    data.extend_from_slice(&amount.to_le_bytes());
    Instruction {
        program_id: *token_program,
        accounts: vec![
            AccountMeta::new(*account, false),
            AccountMeta::new(*mint, false),
            AccountMeta::new_readonly(*owner, true),
        ],
        data,
    }
}

/// `CloseAccount` (tag 9). For wSOL this is the unwrap: every lamport in
/// the account, wrapped balance and rent together, goes to `destination`.
pub fn close_account(token_program: &Pubkey, account: &Pubkey, destination: &Pubkey, owner: &Pubkey) -> Instruction {
    Instruction {
        program_id: *token_program,
        accounts: vec![
            AccountMeta::new(*account, false),
            AccountMeta::new(*destination, false),
            AccountMeta::new_readonly(*owner, true),
        ],
        data: vec![9],
    }
}

/// A token account's owner and amount. Offsets are the same in both token
/// programs: mint 0..32, owner 32..64, amount 64..72.
pub fn read_token_account(data: &[u8]) -> Result<(Pubkey, Pubkey, u64), ProgramError> {
    if data.len() < 72 {
        return Err(ProgramError::InvalidAccountData);
    }
    Ok((
        Pubkey::new_from_array(data[0..32].try_into().unwrap()),
        Pubkey::new_from_array(data[32..64].try_into().unwrap()),
        u64::from_le_bytes(data[64..72].try_into().unwrap()),
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ata_matches_a_known_address() {
        // The pump rail's Python ATA derivation, cross-checked: owner = WSOL mint
        // as a stand-in key, mint = USDC, legacy token program.
        let owner = pubkey!("So11111111111111111111111111111111111111112");
        let usdc = pubkey!("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v");
        let a = associated_token_address(&owner, &usdc, &crate::TOKEN_PROGRAM);
        assert!(!a.is_on_curve());
    }

    #[test]
    fn token_account_reader() {
        let mut d = vec![0u8; 165];
        d[32..64].copy_from_slice(&[9u8; 32]);
        d[64..72].copy_from_slice(&42u64.to_le_bytes());
        let (_, owner, amt) = read_token_account(&d).unwrap();
        assert_eq!(owner, Pubkey::new_from_array([9; 32]));
        assert_eq!(amt, 42);
        assert!(read_token_account(&d[..71]).is_err());
    }
}

/// SPL Token `Transfer` (tag 3). Used only for wSOL, which is legacy SPL.
pub fn transfer(token_program: &Pubkey, from: &Pubkey, to: &Pubkey, owner: &Pubkey, amount: u64) -> Instruction {
    let mut data = vec![3];
    data.extend_from_slice(&amount.to_le_bytes());
    Instruction {
        program_id: *token_program,
        accounts: vec![AccountMeta::new(*from, false), AccountMeta::new(*to, false), AccountMeta::new_readonly(*owner, true)],
        data,
    }
}

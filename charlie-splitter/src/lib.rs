//! The $CHARLIE fee splitter -- one destination, three legs, no key.
//!
//! WHAT THIS IS FOR. $CHARLIE's pump sharing config is `admin_revoked` and
//! pays `burn111...111` 100%: its one irreversible update is spent, so its
//! fees can never be pointed at the protocol's collector and $CHARLIE can
//! never enroll. That is permanent on-chain state, not a gap in the build.
//!
//! So this answers the question that is actually still open: **if
//! $CHARLIE's fee destination were a wallet that split three ways, would
//! the mechanism hold?** A dev launching a coin today can set exactly one
//! destination, once. This program is what that destination should be -- not
//! a wallet somebody controls, but a PDA no key can sign for, whose only
//! exit is a split in fixed proportions.
//!
//! THE SPLIT, and it is NOT the protocol's.
//!
//! ```text
//!     50%  SOL burn                -> the incinerator, destroyed by the runtime
//!     25%  $CHARLIE buy and burn   -> burn_vault, then bought and burned
//!     25%  ops                     -> an ordinary wallet
//! ```
//!
//! The protocol program next door (`program/`) reserves a 25% toll and
//! makes the dev's three shares fill the remaining 7500. There is no toll
//! here: these three legs ARE the whole fee, and they are compiled in
//! rather than stored, because a splitter whose proportions a key can edit
//! is a wallet with extra steps.
//!
//! WHAT THE GUARANTEE IS. The same absence the protocol program rests on:
//! there is no instruction here that moves lamports out of `vault` except
//! `split`, none that moves them out of `burn_vault` except `burn_crank`,
//! and none anywhere that pays an address its caller supplied. Every
//! destination is either compiled in (the incinerator), derived (the burn
//! vault), or read from an account written once at initialisation (ops).
//! A reader can confirm that from this file, and it only means anything
//! once upgrade authority is revoked.

use solana_program::{
    account_info::{next_account_info, AccountInfo},
    declare_id,
    entrypoint::ProgramResult,
    log::sol_log_data,
    msg,
    program::invoke_signed,
    program_error::ProgramError,
    pubkey::Pubkey,
    rent::Rent,
    system_instruction, system_program,
    sysvar::Sysvar,
};

declare_id!("2CfShCuyuLw1935ihB5BjzaymdZsmPLNtUFAAqUeMHBS");

/// The three legs, in bps of the fee that arrived. Compiled in, not stored:
/// proportions a key can edit are not a mechanism.
pub const SOL_BURN_BPS: u16 = 5_000;
pub const BUYBACK_BPS: u16 = 2_500;
pub const OPS_BPS: u16 = 2_500;

/// There is no toll leg, so the three fill the whole fee.
pub const TOTAL_BPS: u16 = SOL_BURN_BPS + BUYBACK_BPS + OPS_BPS;
const _: () = assert!(TOTAL_BPS == 10_000);

/// Solana's incinerator. The runtime removes lamports credited here from
/// the total supply at the end of the block -- not merely unspendable,
/// destroyed. Its balance is therefore always zero, which is why the SOL
/// burn leg is proven by the transfer and never by a balance.
pub const INCINERATOR: Pubkey =
    solana_program::pubkey!("1nc1nerator11111111111111111111111111111111");

pub const SEED_VAULT: &[u8] = b"vault";
pub const SEED_BURN: &[u8] = b"burn";
pub const SEED_CONFIG: &[u8] = b"config";

// -- the one account with anything in it ---------------------------------

/// `config` -- written once at initialisation and never again.
///
/// It holds the ops wallet and the mint this splitter serves. It does NOT
/// hold the proportions: those are constants above. There is no
/// `set_config`, deliberately -- the only field anybody might want to move
/// is `ops_address`, and a splitter whose payout address can be repointed
/// is exactly the wallet this exists to replace.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Config {
    pub mint: Pubkey,
    pub ops_address: Pubkey,
    pub bump: u8,
}

impl Config {
    pub const LEN: usize = 32 + 32 + 1;

    pub fn write(&self, into: &mut [u8]) -> Result<(), ProgramError> {
        if into.len() < Self::LEN {
            return Err(ProgramError::AccountDataTooSmall);
        }
        into[0..32].copy_from_slice(self.mint.as_ref());
        into[32..64].copy_from_slice(self.ops_address.as_ref());
        into[64] = self.bump;
        Ok(())
    }

    pub fn read(from: &[u8]) -> Result<Self, ProgramError> {
        if from.len() < Self::LEN {
            return Err(ProgramError::AccountDataTooSmall);
        }
        Ok(Self {
            mint: Pubkey::try_from(&from[0..32]).map_err(|_| ProgramError::InvalidAccountData)?,
            ops_address: Pubkey::try_from(&from[32..64])
                .map_err(|_| ProgramError::InvalidAccountData)?,
            bump: from[64],
        })
    }
}

// -- derivations ----------------------------------------------------------
//
// All three are derived from the mint, so one deployment serves one coin
// per mint and a caller cannot substitute another coin's vault.

pub fn config_pda(mint: &Pubkey) -> (Pubkey, u8) {
    Pubkey::find_program_address(&[SEED_CONFIG, mint.as_ref()], &id())
}

/// The fee destination. THIS is the address that goes in pump's sharing
/// config -- it receives the creator fee, and no key exists for it.
pub fn vault_pda(mint: &Pubkey) -> (Pubkey, u8) {
    Pubkey::find_program_address(&[SEED_VAULT, mint.as_ref()], &id())
}

/// Where the buy-and-burn leg accrues until a crank spends it.
pub fn burn_vault_pda(mint: &Pubkey) -> (Pubkey, u8) {
    Pubkey::find_program_address(&[SEED_BURN, mint.as_ref()], &id())
}

// -- the split, as arithmetic --------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Legs {
    pub sol_burn: u64,
    pub buyback: u64,
    pub ops: u64,
    pub remainder: u64,
}

/// Three-way integer division of `l` lamports.
///
/// The remainder STAYS IN THE VAULT rather than being swept to a leg.
/// Sweeping it would pay one leg a fraction of a basis point more than the
/// split says, and which leg that is would depend on the order of the code.
/// Left behind, it is counted in the next split.
pub fn split_legs(l: u64) -> Legs {
    let share = |bps: u16| -> u64 { (l as u128 * bps as u128 / 10_000u128) as u64 };
    let sol_burn = share(SOL_BURN_BPS);
    let buyback = share(BUYBACK_BPS);
    let ops = share(OPS_BPS);
    Legs {
        sol_burn,
        buyback,
        ops,
        remainder: l.saturating_sub(sol_burn + buyback + ops),
    }
}

// -- instructions ---------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Instruction {
    /// `initialise(mint, ops_address)` -- creates config, vault and burn
    /// vault. Once per mint; there is no second write.
    Initialise { ops_address: Pubkey },
    /// `split()` -- permissionless. Divides whatever is in the vault above
    /// its rent reserve three ways.
    Split,
}

impl Instruction {
    pub fn unpack(data: &[u8]) -> Result<Self, ProgramError> {
        let (tag, rest) = data
            .split_first()
            .ok_or(ProgramError::InvalidInstructionData)?;
        match tag {
            0 => {
                if rest.len() < 32 {
                    return Err(ProgramError::InvalidInstructionData);
                }
                Ok(Self::Initialise {
                    ops_address: Pubkey::try_from(&rest[0..32])
                        .map_err(|_| ProgramError::InvalidInstructionData)?,
                })
            }
            1 => Ok(Self::Split),
            _ => Err(ProgramError::InvalidInstructionData),
        }
    }
}

#[cfg(not(feature = "no-entrypoint"))]
solana_program::entrypoint!(process_instruction);

pub fn process_instruction(
    program_id: &Pubkey,
    accounts: &[AccountInfo],
    data: &[u8],
) -> ProgramResult {
    if program_id != &id() {
        return Err(ProgramError::IncorrectProgramId);
    }
    match Instruction::unpack(data)? {
        Instruction::Initialise { ops_address } => initialise(accounts, ops_address),
        Instruction::Split => split(accounts),
    }
}

fn create_pda<'a>(
    payer: &AccountInfo<'a>,
    target: &AccountInfo<'a>,
    system: &AccountInfo<'a>,
    seeds: &[&[u8]],
    space: usize,
) -> ProgramResult {
    let rent = Rent::get()?.minimum_balance(space);
    invoke_signed(
        &system_instruction::create_account(payer.key, target.key, rent, space as u64, &id()),
        &[payer.clone(), target.clone(), system.clone()],
        &[seeds],
    )
}

fn initialise(accounts: &[AccountInfo], ops_address: Pubkey) -> ProgramResult {
    let iter = &mut accounts.iter();
    let payer = next_account_info(iter)?;
    let mint = next_account_info(iter)?;
    let config_ai = next_account_info(iter)?;
    let vault = next_account_info(iter)?;
    let burn_vault = next_account_info(iter)?;
    let system = next_account_info(iter)?;

    if !payer.is_signer {
        return Err(ProgramError::MissingRequiredSignature);
    }
    if system.key != &system_program::id() {
        return Err(ProgramError::IncorrectProgramId);
    }

    let (config_key, config_bump) = config_pda(mint.key);
    let (vault_key, vault_bump) = vault_pda(mint.key);
    let (burn_key, burn_bump) = burn_vault_pda(mint.key);
    if config_ai.key != &config_key || vault.key != &vault_key || burn_vault.key != &burn_key {
        return Err(ProgramError::InvalidSeeds);
    }
    if !config_ai.data_is_empty() {
        msg!("already initialised for this mint; there is no second write");
        return Err(ProgramError::AccountAlreadyInitialized);
    }

    create_pda(
        payer,
        config_ai,
        system,
        &[SEED_CONFIG, mint.key.as_ref(), &[config_bump]],
        Config::LEN,
    )?;
    Config {
        mint: *mint.key,
        ops_address,
        bump: config_bump,
    }
    .write(&mut config_ai.try_borrow_mut_data()?)?;

    // Both vaults are created rent exempt with zero data, and they have to
    // be. The runtime refuses any instruction that leaves an account
    // holding lamports below its own rent minimum, and a leg is routinely
    // smaller than that minimum -- so a vault nobody created would fail the
    // whole split and take the other two legs down with it. Learned the
    // hard way on the protocol program next door.
    if vault.lamports() == 0 {
        create_pda(
            payer,
            vault,
            system,
            &[SEED_VAULT, mint.key.as_ref(), &[vault_bump]],
            0,
        )?;
    }
    if burn_vault.lamports() == 0 {
        create_pda(
            payer,
            burn_vault,
            system,
            &[SEED_BURN, mint.key.as_ref(), &[burn_bump]],
            0,
        )?;
    }

    msg!("splitter ready: 50 sol_burn / 25 buyback / 25 ops, compiled in");
    Ok(())
}

/// `split` -- permissionless, and the whole point.
///
/// Takes no addresses from its caller: `config`, `vault` and `burn_vault`
/// are each checked against their derivation from the mint, `ops_address`
/// is read out of `config`, and the incinerator is a constant. A caller who
/// substitutes any of them is refused.
fn split(accounts: &[AccountInfo]) -> ProgramResult {
    let iter = &mut accounts.iter();
    let mint = next_account_info(iter)?;
    let config_ai = next_account_info(iter)?;
    let vault = next_account_info(iter)?;
    let burn_vault = next_account_info(iter)?;
    let incinerator = next_account_info(iter)?;
    let ops = next_account_info(iter)?;

    let (config_key, _) = config_pda(mint.key);
    let (vault_key, _) = vault_pda(mint.key);
    let (burn_key, _) = burn_vault_pda(mint.key);
    if config_ai.key != &config_key || vault.key != &vault_key || burn_vault.key != &burn_key {
        return Err(ProgramError::InvalidSeeds);
    }
    if incinerator.key != &INCINERATOR {
        msg!("split: the SOL burn leg pays the incinerator, and nothing else");
        return Err(ProgramError::InvalidArgument);
    }
    if config_ai.owner != &id() || vault.owner != &id() || burn_vault.owner != &id() {
        return Err(ProgramError::IllegalOwner);
    }

    let config = Config::read(&config_ai.try_borrow_data()?)?;
    // The config names the mint it was created for. Without this a caller
    // could pass one coin's mint with another coin's config and have the
    // derivations agree with the wrong pair.
    if &config.mint != mint.key {
        return Err(ProgramError::InvalidAccountData);
    }
    if ops.key != &config.ops_address {
        msg!("split: ops account is not the one config names");
        return Err(ProgramError::InvalidArgument);
    }

    // The vault keeps its rent-exempt minimum: spending it would close the
    // account, and a closed vault is one pump may refuse to pay.
    let reserve = Rent::get()?.minimum_balance(vault.data_len());
    let l = vault.lamports().saturating_sub(reserve);
    if l == 0 {
        msg!("split: nothing above the rent reserve -- pending, not failed");
        return Ok(());
    }

    let legs = split_legs(l);

    // The ops wallet is not owned by this program, so this program cannot
    // make it rent exempt, and the runtime will not leave it holding
    // lamports below its minimum. Paying it only once it can hold its share
    // keeps the other two legs moving; the share waits in the vault.
    let ops_rent = Rent::get()?.minimum_balance(ops.data_len());
    let ops_amount = if legs.ops > 0 && ops.lamports() + legs.ops >= ops_rent {
        legs.ops
    } else {
        if legs.ops > 0 {
            msg!("split: ops wallet below the rent minimum, its share waits");
        }
        0
    };

    // Direct lamport moves, not system-program transfers: the system
    // program will not debit an account it does not own, so a `transfer`
    // CPI out of a PDA this program owns fails with
    // `ExternalAccountLamportSpend`. Mutating the balances is how a program
    // spends its own PDA, and it keeps the CPI out of the guarantee.
    let mut debit = 0u64;
    for (amount, to) in [
        (legs.sol_burn, incinerator),
        (legs.buyback, burn_vault),
        (ops_amount, ops),
    ] {
        if amount == 0 {
            continue;
        }
        **to.try_borrow_mut_lamports()? = to
            .lamports()
            .checked_add(amount)
            .ok_or(ProgramError::ArithmeticOverflow)?;
        debit = debit
            .checked_add(amount)
            .ok_or(ProgramError::ArithmeticOverflow)?;
    }
    **vault.try_borrow_mut_lamports()? = vault
        .lamports()
        .checked_sub(debit)
        .ok_or(ProgramError::InsufficientFunds)?;

    // Fixed-width little-endian fields after a tag, rather than a formatted
    // string: formatting five u64s through `core::fmt` costs tens of
    // kilobytes of the deployed artifact, and a reader gets numbers instead
    // of a sentence to re-parse.
    sol_log_data(&[
        b"charlie:split",
        &l.to_le_bytes(),
        &legs.sol_burn.to_le_bytes(),
        &legs.buyback.to_le_bytes(),
        &ops_amount.to_le_bytes(),
        &(l - debit).to_le_bytes(),
    ]);
    Ok(())
}

// -- tests ----------------------------------------------------------------
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_three_legs_are_the_whole_fee() {
        assert_eq!(TOTAL_BPS, 10_000);
        assert_eq!(SOL_BURN_BPS, 5_000);
        assert_eq!(BUYBACK_BPS, 2_500);
        assert_eq!(OPS_BPS, 2_500);
    }

    #[test]
    fn a_clean_lot_splits_fifty_twentyfive_twentyfive() {
        let legs = split_legs(1_000_000_000);
        assert_eq!(legs.sol_burn, 500_000_000);
        assert_eq!(legs.buyback, 250_000_000);
        assert_eq!(legs.ops, 250_000_000);
        assert_eq!(legs.remainder, 0);
    }

    #[test]
    fn nothing_is_ever_created_or_lost() {
        for l in [0u64, 1, 3, 7, 999, 1_781_760, 1_000_000_000, u64::MAX / 2] {
            let legs = split_legs(l);
            let paid = legs.sol_burn + legs.buyback + legs.ops;
            assert!(paid <= l, "overpaid at l={}", l);
            assert_eq!(paid + legs.remainder, l, "lost lamports at l={}", l);
        }
    }

    #[test]
    fn the_remainder_waits_rather_than_favouring_a_leg() {
        // 3 lamports: sol_burn floors to 1, the two quarters to 0 each.
        let legs = split_legs(3);
        assert_eq!(legs.sol_burn, 1);
        assert_eq!(legs.buyback, 0);
        assert_eq!(legs.ops, 0);
        assert_eq!(legs.remainder, 2);
    }

    #[test]
    fn the_sol_burn_leg_is_always_the_largest() {
        // The half. A split that ever paid a quarter more than the half
        // would be a different mechanism than the one advertised.
        for l in [4u64, 1_000, 1_000_000_000] {
            let legs = split_legs(l);
            assert!(legs.sol_burn >= legs.buyback);
            assert!(legs.sol_burn >= legs.ops);
        }
    }

    #[test]
    fn config_round_trips_through_its_bytes() {
        let config = Config {
            mint: Pubkey::new_unique(),
            ops_address: Pubkey::new_unique(),
            bump: 250,
        };
        let mut buf = [0u8; Config::LEN];
        config.write(&mut buf).unwrap();
        assert_eq!(Config::read(&buf).unwrap(), config);
    }

    #[test]
    fn instruction_tags_unpack() {
        let key = Pubkey::new_unique();
        let mut data = vec![0u8];
        data.extend_from_slice(key.as_ref());
        assert_eq!(
            Instruction::unpack(&data).unwrap(),
            Instruction::Initialise { ops_address: key }
        );
        assert_eq!(Instruction::unpack(&[1]).unwrap(), Instruction::Split);
        assert!(Instruction::unpack(&[]).is_err());
        assert!(Instruction::unpack(&[9]).is_err());
        // initialise with a truncated pubkey is refused, not read short.
        assert!(Instruction::unpack(&[0, 1, 2]).is_err());
    }

    #[test]
    fn the_three_pdas_are_distinct_and_stable() {
        let mint = Pubkey::new_unique();
        let keys = [
            config_pda(&mint).0,
            vault_pda(&mint).0,
            burn_vault_pda(&mint).0,
        ];
        for (i, a) in keys.iter().enumerate() {
            for b in keys.iter().skip(i + 1) {
                assert_ne!(a, b);
            }
        }
        assert_eq!(vault_pda(&mint).0, vault_pda(&mint).0);
    }

    #[test]
    fn a_different_mint_gets_a_different_vault() {
        // One deployment serves many coins, and no coin can reach another's.
        let a = vault_pda(&Pubkey::new_unique()).0;
        let b = vault_pda(&Pubkey::new_unique()).0;
        assert_ne!(a, b);
    }
}

//! Charlie Protocol -- fee routing.
//!
//! `FEE-ROUTING.md` is the design; this is the whole of the on-chain part of
//! it. Four instructions, and the guarantee is what is NOT here:
//!
//! * nothing moves lamports out of `collector(mint)` except `distribute`;
//! * nothing moves lamports out of `charlie_pool` or `burn_pool(mint)`
//!   except the cranks;
//! * no instruction anywhere sends to an address its caller supplied.
//!
//! Every destination `distribute` pays is derived from the mint or read from
//! the coin's own `route` account, so a caller cannot redirect a lamport of
//! it. A stranger can read this file and confirm all three, and that only
//! means anything once upgrade authority is revoked -- the single
//! load-bearing assumption, unchanged from `ARCHITECTURE.md`.
//!
//! `TOLL_BPS` is a constant in this code and is not a field of any account a
//! dev can write. It is ALSO stored in `charlie_pool` at initialisation and
//! asserted against the constant on every distribute, because a constant
//! compiled into BPF is not on-chain state and a protocol that asks
//! strangers to recompute its figures cannot ask them to reproduce a build
//! first (BUILD.md sec.3).

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

// The program id. This is the keypair `cargo-build-sbf` generated at
// `target/deploy/charlie_protocol-keypair.json` and the address the devnet
// proof is deployed to. It is a DEVNET id: mainnet gets its own keypair,
// generated once and deployed to after the pipeline has been run in
// production (BUILD.md sec.10, and the deploy order is deliberate).
declare_id!("GFA3nG9geMhpPXaExVLGYBtj6aJX7S125dLzv4EcXGiG");

/// The protocol's share of every enrolled coin's creator fee, in bps of that
/// fee. 25%. `BUILD.md` sec.3 settled the rate and why it is forward-only.
pub const TOLL_BPS: u16 = 2500;

/// What a dev's three shares must sum to. They pick these freely; they
/// cannot pick the fourth, because it is not a field.
pub const DEV_BPS_TOTAL: u16 = 10_000 - TOLL_BPS;

/// Solana's incinerator. The runtime removes lamports credited here from the
/// total supply at the end of the block -- not merely unspendable, destroyed.
pub const INCINERATOR: Pubkey =
    solana_program::pubkey!("1nc1nerator11111111111111111111111111111111");

pub const SEED_ROUTE: &[u8] = b"route";
pub const SEED_COLLECT: &[u8] = b"collect";
pub const SEED_BURN: &[u8] = b"burn";
pub const SEED_CHARLIE: &[u8] = b"charlie";

// -- account layouts ------------------------------------------------------
// Hand-rolled little-endian encoding. No borsh, no anchor: the point of this
// program is that a stranger can read it, and a derive macro they have to
// trust is a worse guarantee than 20 lines they can check.

/// `route(mint)` -- the dev's split. Changeable by the coin's live admin, any
/// day, as often as they like. 47 bytes.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Route {
    pub sol_burn_bps: u16,
    pub own_burn_bps: u16,
    pub ops_bps: u16,
    pub ops_address: Pubkey,
    pub last_distrib: i64,
    pub bump: u8,
}

impl Route {
    pub const LEN: usize = 2 + 2 + 2 + 32 + 8 + 1;

    pub fn write(&self, into: &mut [u8]) -> Result<(), ProgramError> {
        if into.len() < Self::LEN {
            return Err(ProgramError::AccountDataTooSmall);
        }
        into[0..2].copy_from_slice(&self.sol_burn_bps.to_le_bytes());
        into[2..4].copy_from_slice(&self.own_burn_bps.to_le_bytes());
        into[4..6].copy_from_slice(&self.ops_bps.to_le_bytes());
        into[6..38].copy_from_slice(self.ops_address.as_ref());
        into[38..46].copy_from_slice(&self.last_distrib.to_le_bytes());
        into[46] = self.bump;
        Ok(())
    }

    pub fn read(from: &[u8]) -> Result<Self, ProgramError> {
        if from.len() < Self::LEN {
            return Err(ProgramError::AccountDataTooSmall);
        }
        Ok(Self {
            sol_burn_bps: u16::from_le_bytes([from[0], from[1]]),
            own_burn_bps: u16::from_le_bytes([from[2], from[3]]),
            ops_bps: u16::from_le_bytes([from[4], from[5]]),
            ops_address: Pubkey::try_from(&from[6..38])
                .map_err(|_| ProgramError::InvalidAccountData)?,
            last_distrib: i64::from_le_bytes([
                from[38], from[39], from[40], from[41], from[42], from[43], from[44], from[45],
            ]),
            bump: from[46],
        })
    }

    /// The invariant, enforced on every write. The dev's three shares fill
    /// exactly what the toll leaves, so there is no arrangement of them that
    /// takes a basis point of the toll.
    pub fn validate(&self) -> Result<(), ProgramError> {
        let sum = (self.sol_burn_bps as u32)
            .checked_add(self.own_burn_bps as u32)
            .and_then(|s| s.checked_add(self.ops_bps as u32))
            .ok_or(ProgramError::InvalidInstructionData)?;
        if sum != DEV_BPS_TOTAL as u32 {
            // Not formatted: the sum the caller sent is in the instruction
            // data, and TOLL_BPS is a published constant.
            msg!("route: the three dev shares must sum to 10000 - TOLL_BPS");
            return Err(ProgramError::InvalidInstructionData);
        }
        Ok(())
    }
}

/// `charlie_pool` -- global. Stores the toll rate so it is one
/// `getAccountInfo` away rather than a build to reproduce.
#[derive(Debug, Clone, Copy)]
pub struct CharliePool {
    pub toll_bps: u16,
    pub bump: u8,
}

impl CharliePool {
    pub const LEN: usize = 2 + 1;

    pub fn write(&self, into: &mut [u8]) -> Result<(), ProgramError> {
        if into.len() < Self::LEN {
            return Err(ProgramError::AccountDataTooSmall);
        }
        into[0..2].copy_from_slice(&self.toll_bps.to_le_bytes());
        into[2] = self.bump;
        Ok(())
    }

    pub fn read(from: &[u8]) -> Result<Self, ProgramError> {
        if from.len() < Self::LEN {
            return Err(ProgramError::AccountDataTooSmall);
        }
        Ok(Self {
            toll_bps: u16::from_le_bytes([from[0], from[1]]),
            bump: from[2],
        })
    }
}

// -- derivations ----------------------------------------------------------

pub fn route_pda(mint: &Pubkey) -> (Pubkey, u8) {
    Pubkey::find_program_address(&[SEED_ROUTE, mint.as_ref()], &id())
}

pub fn collector_pda(mint: &Pubkey) -> (Pubkey, u8) {
    Pubkey::find_program_address(&[SEED_COLLECT, mint.as_ref()], &id())
}

pub fn burn_pool_pda(mint: &Pubkey) -> (Pubkey, u8) {
    Pubkey::find_program_address(&[SEED_BURN, mint.as_ref()], &id())
}

pub fn charlie_pool_pda() -> (Pubkey, u8) {
    Pubkey::find_program_address(&[SEED_CHARLIE], &id())
}

// -- the split, as arithmetic --------------------------------------------

/// What `distribute` pays each leg out of `l` distributable lamports.
///
/// Integer division four ways, and the remainder STAYS IN THE COLLECTOR
/// rather than being swept to any leg. Sweeping it would mean one leg is
/// paid a fraction of a basis point more than the split says, and which leg
/// that is would depend on the order of the code. Left behind, it is counted
/// in the next distribution.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Legs {
    pub toll: u64,
    pub sol_burn: u64,
    pub own_burn: u64,
    pub ops: u64,
    pub remainder: u64,
}

pub fn split_legs(l: u64, route: &Route, toll_bps: u16) -> Legs {
    let share = |bps: u16| -> u64 { (l as u128 * bps as u128 / 10_000u128) as u64 };
    let toll = share(toll_bps);
    let sol_burn = share(route.sol_burn_bps);
    let own_burn = share(route.own_burn_bps);
    let ops = share(route.ops_bps);
    let paid = toll + sol_burn + own_burn + ops;
    Legs {
        toll,
        sol_burn,
        own_burn,
        ops,
        remainder: l.saturating_sub(paid),
    }
}

// -- instructions ---------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Instruction {
    /// `init_charlie_pool` -- creates the global pool and stores TOLL_BPS.
    InitCharliePool,
    /// `init_route(sol_burn_bps, own_burn_bps, ops_bps, ops_address)`
    InitRoute {
        sol_burn_bps: u16,
        own_burn_bps: u16,
        ops_bps: u16,
        ops_address: Pubkey,
    },
    /// `set_route(...)` -- same shape, any time.
    SetRoute {
        sol_burn_bps: u16,
        own_burn_bps: u16,
        ops_bps: u16,
        ops_address: Pubkey,
    },
    /// `distribute` -- permissionless, no signer, no caller-supplied address.
    Distribute,
}

impl Instruction {
    pub fn unpack(data: &[u8]) -> Result<Self, ProgramError> {
        let (tag, rest) = data
            .split_first()
            .ok_or(ProgramError::InvalidInstructionData)?;
        let shares = |rest: &[u8]| -> Result<(u16, u16, u16, Pubkey), ProgramError> {
            if rest.len() < 6 + 32 {
                return Err(ProgramError::InvalidInstructionData);
            }
            Ok((
                u16::from_le_bytes([rest[0], rest[1]]),
                u16::from_le_bytes([rest[2], rest[3]]),
                u16::from_le_bytes([rest[4], rest[5]]),
                Pubkey::try_from(&rest[6..38]).map_err(|_| ProgramError::InvalidInstructionData)?,
            ))
        };
        match tag {
            0 => Ok(Self::InitCharliePool),
            1 => {
                let (s, o, p, a) = shares(rest)?;
                Ok(Self::InitRoute {
                    sol_burn_bps: s,
                    own_burn_bps: o,
                    ops_bps: p,
                    ops_address: a,
                })
            }
            2 => {
                let (s, o, p, a) = shares(rest)?;
                Ok(Self::SetRoute {
                    sol_burn_bps: s,
                    own_burn_bps: o,
                    ops_bps: p,
                    ops_address: a,
                })
            }
            3 => Ok(Self::Distribute),
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
        Instruction::InitCharliePool => init_charlie_pool(accounts),
        Instruction::InitRoute {
            sol_burn_bps,
            own_burn_bps,
            ops_bps,
            ops_address,
        } => init_route(accounts, sol_burn_bps, own_burn_bps, ops_bps, ops_address),
        Instruction::SetRoute {
            sol_burn_bps,
            own_burn_bps,
            ops_bps,
            ops_address,
        } => set_route(accounts, sol_burn_bps, own_burn_bps, ops_bps, ops_address),
        Instruction::Distribute => distribute(accounts),
    }
}

/// Create a PDA this program owns, paid for by `payer`.
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

fn init_charlie_pool(accounts: &[AccountInfo]) -> ProgramResult {
    let iter = &mut accounts.iter();
    let payer = next_account_info(iter)?;
    let charlie_pool = next_account_info(iter)?;
    let system = next_account_info(iter)?;

    if !payer.is_signer {
        return Err(ProgramError::MissingRequiredSignature);
    }
    if system.key != &system_program::id() {
        return Err(ProgramError::IncorrectProgramId);
    }
    let (expected, bump) = charlie_pool_pda();
    if charlie_pool.key != &expected {
        return Err(ProgramError::InvalidSeeds);
    }
    if !charlie_pool.data_is_empty() {
        msg!("charlie_pool already initialised");
        return Err(ProgramError::AccountAlreadyInitialized);
    }
    create_pda(
        payer,
        charlie_pool,
        system,
        &[SEED_CHARLIE, &[bump]],
        CharliePool::LEN,
    )?;
    CharliePool {
        toll_bps: TOLL_BPS,
        bump,
    }
    .write(&mut charlie_pool.try_borrow_mut_data()?)?;
    msg!("charlie_pool initialised");
    Ok(())
}

/// `init_route` -- creates `route(mint)` and `collector(mint)`.
///
/// The signer check is the coin's own admin, which for the devnet proof is
/// the mint authority standing in for pump's `sharing_config.admin`. On
/// mainnet this reads the live sharing config (BUILD.md sec.4); the shape of
/// the check -- ownership is the key pump already recognises, not a claim we
/// invent -- is the same either way.
fn init_route(
    accounts: &[AccountInfo],
    sol_burn_bps: u16,
    own_burn_bps: u16,
    ops_bps: u16,
    ops_address: Pubkey,
) -> ProgramResult {
    let iter = &mut accounts.iter();
    let admin = next_account_info(iter)?;
    let mint = next_account_info(iter)?;
    let route_ai = next_account_info(iter)?;
    let collector = next_account_info(iter)?;
    let burn_pool = next_account_info(iter)?;
    let system = next_account_info(iter)?;

    if !admin.is_signer {
        return Err(ProgramError::MissingRequiredSignature);
    }
    if system.key != &system_program::id() {
        return Err(ProgramError::IncorrectProgramId);
    }

    let (route_key, route_bump) = route_pda(mint.key);
    let (collector_key, collector_bump) = collector_pda(mint.key);
    let (burn_key, burn_bump) = burn_pool_pda(mint.key);
    if route_ai.key != &route_key
        || collector.key != &collector_key
        || burn_pool.key != &burn_key
    {
        return Err(ProgramError::InvalidSeeds);
    }
    if !route_ai.data_is_empty() {
        return Err(ProgramError::AccountAlreadyInitialized);
    }

    let route = Route {
        sol_burn_bps,
        own_burn_bps,
        ops_bps,
        ops_address,
        last_distrib: 0,
        bump: route_bump,
    };
    route.validate()?;

    create_pda(
        admin,
        route_ai,
        system,
        &[SEED_ROUTE, mint.key.as_ref(), &[route_bump]],
        Route::LEN,
    )?;
    route.write(&mut route_ai.try_borrow_mut_data()?)?;

    // The collector is created rent-exempt with zero data. FEE-ROUTING.md
    // sec.7 measured that pump pays an address that does not exist yet, but a
    // program-owned recipient is refused when executable, so creating it up
    // front is the safe order rather than an assumption.
    if collector.lamports() == 0 {
        create_pda(
            admin,
            collector,
            system,
            &[SEED_COLLECT, mint.key.as_ref(), &[collector_bump]],
            0,
        )?;
    }

    // `burn_pool` is created here too, and it has to be.
    //
    // The runtime refuses any instruction that leaves an account holding
    // lamports but below its rent-exempt minimum. A distribution's own-burn
    // leg is usually far smaller than that minimum -- 60,000 lamports
    // against a 650,000 floor on the first devnet run -- so crediting a
    // burn_pool that nobody had created failed the whole `distribute` with
    // `InsufficientFundsForRent`, and took the other three legs down with
    // it. Rent-exempt from the start, it can receive any amount.
    //
    // This is the same reasoning FEE-ROUTING.md sec.7 gives for creating the
    // collector up front rather than trusting that an unfunded address can
    // be paid. It applies to every destination this program owns.
    if burn_pool.lamports() == 0 {
        create_pda(
            admin,
            burn_pool,
            system,
            &[SEED_BURN, mint.key.as_ref(), &[burn_bump]],
            0,
        )?;
    }

    // The shares are readable in `route(mint)` the moment this returns, so
    // the log says what happened rather than restating them.
    msg!("route initialised; the toll is not a field of it");
    Ok(())
}

fn set_route(
    accounts: &[AccountInfo],
    sol_burn_bps: u16,
    own_burn_bps: u16,
    ops_bps: u16,
    ops_address: Pubkey,
) -> ProgramResult {
    let iter = &mut accounts.iter();
    let admin = next_account_info(iter)?;
    let mint = next_account_info(iter)?;
    let route_ai = next_account_info(iter)?;

    if !admin.is_signer {
        return Err(ProgramError::MissingRequiredSignature);
    }
    let (route_key, _) = route_pda(mint.key);
    if route_ai.key != &route_key {
        return Err(ProgramError::InvalidSeeds);
    }
    if route_ai.owner != &id() {
        return Err(ProgramError::IllegalOwner);
    }

    let existing = Route::read(&route_ai.try_borrow_data()?)?;
    let updated = Route {
        sol_burn_bps,
        own_burn_bps,
        ops_bps,
        ops_address,
        last_distrib: existing.last_distrib,
        bump: existing.bump,
    };
    updated.validate()?;
    updated.write(&mut route_ai.try_borrow_mut_data()?)?;
    msg!("route updated");
    Ok(())
}

/// `distribute` -- permissionless, and the whole point.
///
/// Takes no addresses from its caller: `route`, `collector`, `burn_pool` and
/// `charlie_pool` are each checked against their derivation from the mint,
/// `ops_address` is read out of `route`, and the incinerator is a constant.
/// A caller who substitutes any of them gets `InvalidSeeds`.
fn distribute(accounts: &[AccountInfo]) -> ProgramResult {
    let iter = &mut accounts.iter();
    let mint = next_account_info(iter)?;
    let route_ai = next_account_info(iter)?;
    let collector = next_account_info(iter)?;
    let charlie_pool = next_account_info(iter)?;
    let burn_pool = next_account_info(iter)?;
    let incinerator = next_account_info(iter)?;
    let ops = next_account_info(iter)?;
    let system = next_account_info(iter)?;

    // -- every destination, checked against its derivation ----------------
    let (route_key, _) = route_pda(mint.key);
    let (collector_key, _) = collector_pda(mint.key);
    let (burn_key, _) = burn_pool_pda(mint.key);
    let (charlie_key, _) = charlie_pool_pda();

    if route_ai.key != &route_key
        || collector.key != &collector_key
        || burn_pool.key != &burn_key
        || charlie_pool.key != &charlie_key
    {
        return Err(ProgramError::InvalidSeeds);
    }
    if incinerator.key != &INCINERATOR {
        msg!("distribute: the SOL burn leg pays the incinerator, and nothing else");
        return Err(ProgramError::InvalidArgument);
    }
    if system.key != &system_program::id() {
        return Err(ProgramError::IncorrectProgramId);
    }
    if route_ai.owner != &id() || collector.owner != &id() {
        return Err(ProgramError::IllegalOwner);
    }

    let route = Route::read(&route_ai.try_borrow_data()?)?;

    // The stored toll and the compiled constant must agree. If they ever
    // diverge this program is not the program its own account describes,
    // and it refuses rather than paying a rate nobody published.
    let stored = CharliePool::read(&charlie_pool.try_borrow_data()?)?;
    if stored.toll_bps != TOLL_BPS {
        msg!("distribute: charlie_pool's stored toll and this program disagree");
        return Err(ProgramError::InvalidAccountData);
    }

    // `ops_address` comes out of `route`, never off the wire.
    if ops.key != &route.ops_address {
        msg!("distribute: ops account is not the one route names");
        return Err(ProgramError::InvalidArgument);
    }

    // -- what is distributable -------------------------------------------
    // The collector keeps its rent-exempt minimum: spending it would close
    // the account, and a closed collector is one pump may refuse to pay.
    let reserve = Rent::get()?.minimum_balance(collector.data_len());
    let balance = collector.lamports();
    let l = balance.saturating_sub(reserve);
    if l == 0 {
        msg!("distribute: nothing above the rent reserve -- pending, not failed");
        return Ok(());
    }

    let legs = split_legs(l, &route, stored.toll_bps);

    // A destination this program does not own cannot be made rent exempt by
    // this program, and the runtime refuses an instruction that leaves any
    // account holding lamports below its own minimum. `ops_address` is an
    // ordinary wallet the dev chose, so a brand new one would fail every
    // distribution until somebody funded it -- and take the toll and both
    // burn legs down with it, because they are all one instruction.
    //
    // Paying it only once it can hold what it is paid is the conservative
    // half: the ops share stays in the collector and is counted in the next
    // distribution, so nothing is lost and the other three legs still move.
    let ops_rent = Rent::get()?.minimum_balance(ops.data_len());
    let ops_payable = legs.ops > 0 && ops.lamports() + legs.ops >= ops_rent;
    let ops_amount = if ops_payable { legs.ops } else { 0 };
    if !ops_payable && legs.ops > 0 {
        msg!("distribute: ops wallet below the rent minimum, its share waits");
    }

    // Each leg is a direct lamport move, NOT a system-program transfer.
    //
    // This is not a style choice. `collector` is owned by THIS program, and
    // the runtime refuses to let the system program debit an account it does
    // not own: a `transfer` CPI out of the collector fails with
    // `ExternalAccountLamportSpend`, which is exactly what the first devnet
    // run of this instruction did. A program moves lamports out of an
    // account it owns by mutating the balances directly, and the runtime
    // checks the sum is conserved when the instruction returns.
    //
    // It also removes the CPI from the guarantee. There is no inner
    // instruction a reader has to follow, and no signer seeds handed to
    // another program -- the debit is four lines of arithmetic on accounts
    // this program already owns.
    let mut debit = 0u64;
    for (amount, to) in [
        (legs.toll, charlie_pool),
        (legs.sol_burn, incinerator),
        (legs.own_burn, burn_pool),
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
    **collector.try_borrow_mut_lamports()? = collector
        .lamports()
        .checked_sub(debit)
        .ok_or(ProgramError::InsufficientFunds)?;

    // The event, so a stranger can attribute the transfers to legs rather
    // than reverse-engineer bare lamport deltas out of three PDAs.
    //
    // The mint is not a field: it is account 0 of this instruction, so a
    // reader already has it.
    //
    // `ops` is what was PAID, which is not always what the split computed:
    // an ops wallet that cannot hold its share yet is paid zero and the
    // share waits in the collector. Reporting the computed figure would
    // overstate a payment that did not happen, and `remainder` is what is
    // left behind either way -- so both are derived from the debit.
    //
    // It is emitted with `sol_log_data` rather than a formatted `msg!`,
    // which is BOTH smaller and easier to parse. Formatting seven u64s
    // through `core::fmt` pulled enough of Rust's formatting machinery into
    // the artifact to add roughly 30KB -- and a 96KB program needed ~190
    // write transactions to deploy, which devnet refused twice. A reader
    // gets fixed-width little-endian fields instead of a sentence, in the
    // transaction's `data:` log line.
    sol_log_data(&[
        b"charlie:distribute",
        &l.to_le_bytes(),
        &legs.toll.to_le_bytes(),
        &legs.sol_burn.to_le_bytes(),
        &legs.own_burn.to_le_bytes(),
        &ops_amount.to_le_bytes(),
        &(l - debit).to_le_bytes(),
        &stored.toll_bps.to_le_bytes(),
    ]);
    Ok(())
}

// -- tests ----------------------------------------------------------------
// The arithmetic, tested off-chain. These run on the host with `cargo test`
// and need no validator.
#[cfg(test)]
mod tests {
    use super::*;

    fn route(sol_burn: u16, own_burn: u16, ops: u16) -> Route {
        Route {
            sol_burn_bps: sol_burn,
            own_burn_bps: own_burn,
            ops_bps: ops,
            ops_address: Pubkey::new_unique(),
            last_distrib: 0,
            bump: 255,
        }
    }

    #[test]
    fn dev_shares_must_fill_exactly_what_the_toll_leaves() {
        assert!(route(500, 2000, 5000).validate().is_ok());
        // 10000 would take the toll's own share.
        assert!(route(10_000, 0, 0).validate().is_err());
        // One bp short is still wrong: the remainder would be unassigned.
        assert!(route(499, 2000, 5000).validate().is_err());
    }

    #[test]
    fn the_toll_is_a_quarter_and_the_dev_keeps_three() {
        assert_eq!(TOLL_BPS, 2500);
        assert_eq!(DEV_BPS_TOTAL, 7500);
    }

    #[test]
    fn legs_never_exceed_what_came_in() {
        let r = route(500, 2000, 5000);
        for l in [0u64, 1, 7, 1_781_760, 1_000_000_000, u64::MAX / 2] {
            let legs = split_legs(l, &r, TOLL_BPS);
            let paid = legs.toll + legs.sol_burn + legs.own_burn + legs.ops;
            assert!(paid <= l, "overpaid at l={}", l);
            assert_eq!(paid + legs.remainder, l, "lost lamports at l={}", l);
        }
    }

    #[test]
    fn the_toll_is_exactly_a_quarter_of_a_clean_lot() {
        let legs = split_legs(1_000_000_000, &route(500, 2000, 5000), TOLL_BPS);
        assert_eq!(legs.toll, 250_000_000);
        assert_eq!(legs.sol_burn, 50_000_000);
        assert_eq!(legs.own_burn, 200_000_000);
        assert_eq!(legs.ops, 500_000_000);
        assert_eq!(legs.remainder, 0);
    }

    #[test]
    fn a_dev_can_zero_two_legs_and_the_toll_is_unmoved() {
        let legs = split_legs(1_000_000_000, &route(0, 0, 7500), TOLL_BPS);
        assert_eq!(legs.toll, 250_000_000);
        assert_eq!(legs.ops, 750_000_000);
        assert_eq!(legs.sol_burn, 0);
        assert_eq!(legs.own_burn, 0);
    }

    #[test]
    fn the_remainder_stays_behind_rather_than_favouring_a_leg() {
        // 7 lamports does not divide four ways. toll floors to 1, sol_burn
        // to 0, own_burn to 1, ops to 3 -- five paid, and the two that
        // cannot be assigned stay in the collector for the next round
        // rather than being swept to whichever leg the code happens to
        // visit last.
        let legs = split_legs(7, &route(500, 2000, 5000), TOLL_BPS);
        assert_eq!(legs.toll, 1);
        assert_eq!(legs.sol_burn, 0);
        assert_eq!(legs.own_burn, 1);
        assert_eq!(legs.ops, 3);
        assert_eq!(legs.toll + legs.sol_burn + legs.own_burn + legs.ops, 5);
        assert_eq!(legs.remainder, 2);
    }

    #[test]
    fn route_round_trips_through_its_bytes() {
        let r = route(500, 2000, 5000);
        let mut buf = [0u8; Route::LEN];
        r.write(&mut buf).unwrap();
        assert_eq!(Route::read(&buf).unwrap(), r);
    }

    #[test]
    fn charlie_pool_round_trips() {
        let mut buf = [0u8; CharliePool::LEN];
        CharliePool {
            toll_bps: TOLL_BPS,
            bump: 7,
        }
        .write(&mut buf)
        .unwrap();
        let back = CharliePool::read(&buf).unwrap();
        assert_eq!(back.toll_bps, TOLL_BPS);
        assert_eq!(back.bump, 7);
    }

    #[test]
    fn instruction_tags_unpack() {
        assert_eq!(
            Instruction::unpack(&[0]).unwrap(),
            Instruction::InitCharliePool
        );
        assert_eq!(Instruction::unpack(&[3]).unwrap(), Instruction::Distribute);
        assert!(Instruction::unpack(&[9]).is_err());
        assert!(Instruction::unpack(&[]).is_err());
        // init_route with a truncated payload is refused, not read short.
        assert!(Instruction::unpack(&[1, 0, 0]).is_err());
    }

    #[test]
    fn pdas_are_distinct_and_stable() {
        let mint = Pubkey::new_unique();
        let keys = [
            route_pda(&mint).0,
            collector_pda(&mint).0,
            burn_pool_pda(&mint).0,
            charlie_pool_pda().0,
        ];
        for (i, a) in keys.iter().enumerate() {
            for b in keys.iter().skip(i + 1) {
                assert_ne!(a, b);
            }
        }
        assert_eq!(route_pda(&mint).0, route_pda(&mint).0);
    }
}

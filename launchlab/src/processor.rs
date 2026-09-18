use crate::{
    collect_pda, guards, id,
    instruction::{Instruction, LaunchArgs},
    platform_pda, quote_pda, raydium, route_pda, signer_pda,
    state::{split_legs, Platform, QuoteCfg, Route},
    raydium::cpmm, tokenix, CPMM_PROGRAM, CREATOR_FEE_RATE, LOCK_PROGRAM, LP_CHARLIE_BPS, BURN_SCALE, CREATOR_SCALE, INCINERATOR, LAUNCHLAB_PROGRAM, PLATFORM_FEE_RATE,
    PLATFORM_SCALE, TOKEN_2022_PROGRAM, TOKEN_PROGRAM,
};
use solana_program::{
    account_info::{next_account_info, AccountInfo},
    bpf_loader_upgradeable,
    entrypoint::ProgramResult,
    msg,
    log::sol_log_data,
    program::{invoke, invoke_signed},
    program_error::ProgramError,
    pubkey::Pubkey,
    rent::Rent,
    system_instruction, system_program,
    sysvar::Sysvar,
};

pub fn process_instruction(program_id: &Pubkey, accounts: &[AccountInfo], data: &[u8]) -> ProgramResult {
    if program_id != &id() {
        return Err(ProgramError::IncorrectProgramId);
    }
    match Instruction::unpack(data)? {
        Instruction::InitPlatform { admin, charlie_mint, charlie_pool } => {
            init_platform(accounts, admin, charlie_mint, charlie_pool)
        }
        Instruction::SetQuote { enabled, oracle, max_slippage_bps, max_staleness_s, min_crank } => {
            set_quote(accounts, enabled, oracle, max_slippage_bps, max_staleness_s, min_crank)
        }
        Instruction::SetRoute { sol_burn_bps, own_burn_bps, ops_bps, ops_address } => {
            set_route(accounts, sol_burn_bps, own_burn_bps, ops_bps, ops_address)
        }
        Instruction::SetPaused { paused } => set_paused(accounts, paused),
        Instruction::SetAdmin { new_admin } => set_admin(accounts, new_admin),
        Instruction::CreateRaydiumPlatform { fund_lamports } => create_raydium_platform(accounts, fund_lamports),
        Instruction::Launch(args) => launch(accounts, args),
        Instruction::CrankCurve => crank_curve(accounts),
        Instruction::CrankAmmCreator => crank_amm_creator(accounts),
        Instruction::CrankLp { lp_fee_amount } => crank_lp(accounts, lp_fee_amount),
        Instruction::CrankPlatform => crank_platform(accounts),
        Instruction::ProposePlatformChange | Instruction::ApplyPlatformChange => not_built(9),
    }
}

fn not_built(step: u8) -> ProgramResult {
    msg!("not built yet: LAUNCHLAB-RAIL.md section 8, step {}", step);
    Err(ProgramError::InvalidInstructionData)
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

/// The upgrade authority recorded in this program's ProgramData account:
/// u32 tag (3) | u64 slot | u8 option | 32-byte authority.
pub fn upgrade_authority(program_data: &[u8]) -> Result<Option<Pubkey>, ProgramError> {
    if program_data.len() < 45 || program_data[0..4] != 3u32.to_le_bytes() {
        return Err(ProgramError::InvalidAccountData);
    }
    Ok(match program_data[12] {
        0 => None,
        1 => Some(Pubkey::new_from_array(program_data[13..45].try_into().unwrap())),
        _ => return Err(ProgramError::InvalidAccountData),
    })
}

fn read_platform(ai: &AccountInfo) -> Result<Platform, ProgramError> {
    if ai.key != &platform_pda().0 {
        return Err(ProgramError::InvalidSeeds);
    }
    if ai.owner != &id() {
        return Err(ProgramError::IllegalOwner);
    }
    Platform::read(&ai.try_borrow_data()?)
}

fn require_admin(admin: &AccountInfo, platform: &Platform) -> ProgramResult {
    if !admin.is_signer {
        return Err(ProgramError::MissingRequiredSignature);
    }
    if admin.key != &platform.admin {
        msg!("not the platform admin");
        return Err(ProgramError::InvalidArgument);
    }
    Ok(())
}

/// Once. Only this program's upgrade authority can call it, so nobody can
/// front-run the deploy and install themselves as admin.
fn init_platform(accounts: &[AccountInfo], admin: Pubkey, charlie_mint: Pubkey, charlie_pool: Pubkey) -> ProgramResult {
    let it = &mut accounts.iter();
    let payer = next_account_info(it)?;
    let platform_ai = next_account_info(it)?;
    let program_data = next_account_info(it)?;
    let system = next_account_info(it)?;

    if !payer.is_signer {
        return Err(ProgramError::MissingRequiredSignature);
    }
    if system.key != &system_program::id() {
        return Err(ProgramError::IncorrectProgramId);
    }
    let (pd_key, _) = Pubkey::find_program_address(&[id().as_ref()], &bpf_loader_upgradeable::id());
    if program_data.key != &pd_key || program_data.owner != &bpf_loader_upgradeable::id() {
        return Err(ProgramError::InvalidSeeds);
    }
    if upgrade_authority(&program_data.try_borrow_data()?)? != Some(*payer.key) {
        msg!("init_platform: only the upgrade authority");
        return Err(ProgramError::MissingRequiredSignature);
    }
    let (key, bump) = platform_pda();
    if platform_ai.key != &key {
        return Err(ProgramError::InvalidSeeds);
    }
    if !platform_ai.data_is_empty() {
        return Err(ProgramError::AccountAlreadyInitialized);
    }
    if admin == Pubkey::default() || charlie_mint == Pubkey::default() || charlie_pool == Pubkey::default() {
        return Err(ProgramError::InvalidArgument);
    }
    create_pda(payer, platform_ai, system, &[crate::SEED_PLATFORM, &[bump]], Platform::LEN)?;
    Platform {
        admin,
        raydium_platform_config: Pubkey::default(),
        charlie_mint,
        charlie_pool,
        paused: false,
        bump,
        signer_bump: signer_pda().1,
    }
    .write(&mut platform_ai.try_borrow_mut_data()?)?;
    msg!("ll_platform initialised");
    Ok(())
}

fn set_quote(
    accounts: &[AccountInfo],
    enabled: bool,
    oracle: Pubkey,
    max_slippage_bps: u16,
    max_staleness_s: u32,
    min_crank: u64,
) -> ProgramResult {
    let it = &mut accounts.iter();
    let admin = next_account_info(it)?;
    let platform_ai = next_account_info(it)?;
    let quote_ai = next_account_info(it)?;
    let mint = next_account_info(it)?;
    let system = next_account_info(it)?;

    let platform = read_platform(platform_ai)?;
    require_admin(admin, &platform)?;
    if system.key != &system_program::id() {
        return Err(ProgramError::IncorrectProgramId);
    }
    if mint.owner != &TOKEN_PROGRAM && mint.owner != &TOKEN_2022_PROGRAM {
        return Err(ProgramError::IllegalOwner);
    }
    let (key, bump) = quote_pda(mint.key);
    if quote_ai.key != &key {
        return Err(ProgramError::InvalidSeeds);
    }

    // Decimals and token program come from the mint itself. Enabling runs
    // the same guard every crank runs, so a quote with a live hook or a
    // pause cannot be switched on in the first place.
    let st = if enabled {
        guards::check_quote_mint(&mint.try_borrow_data()?)?
    } else {
        guards::read_quote_mint(&mint.try_borrow_data()?)?
    };
    let cfg = QuoteCfg {
        quote_mint: *mint.key,
        token_program: *mint.owner,
        decimals: st.decimals,
        enabled,
        oracle,
        max_slippage_bps,
        max_staleness_s,
        min_crank,
        bump,
    };
    cfg.validate()?;
    if quote_ai.data_is_empty() {
        create_pda(admin, quote_ai, system, &[crate::SEED_QUOTE, mint.key.as_ref(), &[bump]], QuoteCfg::LEN)?;
    } else if quote_ai.owner != &id() {
        return Err(ProgramError::IllegalOwner);
    }
    cfg.write(&mut quote_ai.try_borrow_mut_data()?)?;
    msg!("quote_cfg set");
    Ok(())
}

fn set_route(accounts: &[AccountInfo], sol_burn_bps: u16, own_burn_bps: u16, ops_bps: u16, ops_address: Pubkey) -> ProgramResult {
    let it = &mut accounts.iter();
    let admin = next_account_info(it)?;
    let route_ai = next_account_info(it)?;

    if !admin.is_signer {
        return Err(ProgramError::MissingRequiredSignature);
    }
    if route_ai.owner != &id() {
        return Err(ProgramError::IllegalOwner);
    }
    let existing = Route::read(&route_ai.try_borrow_data()?)?;
    if route_ai.key != &route_pda(&existing.mint).0 {
        return Err(ProgramError::InvalidSeeds);
    }
    if admin.key != &existing.admin {
        msg!("set_route: not the coin's admin");
        return Err(ProgramError::InvalidArgument);
    }
    // Only the four fields a dev may choose change; totals, pools and the
    // quote are carried over untouched.
    Route { sol_burn_bps, own_burn_bps, ops_bps, ops_address, ..existing }
        .write(&mut route_ai.try_borrow_mut_data()?)?;
    msg!("ll_route updated");
    Ok(())
}

fn set_paused(accounts: &[AccountInfo], paused: bool) -> ProgramResult {
    let it = &mut accounts.iter();
    let admin = next_account_info(it)?;
    let platform_ai = next_account_info(it)?;
    let platform = read_platform(platform_ai)?;
    require_admin(admin, &platform)?;
    Platform { paused, ..platform }.write(&mut platform_ai.try_borrow_mut_data()?)?;
    msg!("ll_platform paused={}", paused);
    Ok(())
}

fn set_admin(accounts: &[AccountInfo], new_admin: Pubkey) -> ProgramResult {
    let it = &mut accounts.iter();
    let admin = next_account_info(it)?;
    let platform_ai = next_account_info(it)?;
    let platform = read_platform(platform_ai)?;
    require_admin(admin, &platform)?;
    if new_admin == Pubkey::default() {
        return Err(ProgramError::InvalidArgument);
    }
    Platform { admin: new_admin, ..platform }.write(&mut platform_ai.try_borrow_mut_data()?)?;
    msg!("ll_platform admin changed");
    Ok(())
}

/// What `launch` requires the dev to move into `ll_collect`: enough for the
/// rent of the quote-side token account a claim may have to create, twice
/// over, plus the System account's own rent floor.
pub const MIN_COLLECT_FUND: u64 = 10_000_000;

fn expect(ai: &AccountInfo, key: &Pubkey, what: &str) -> ProgramResult {
    if ai.key != key {
        msg!("wrong account: {}", what);
        return Err(ProgramError::InvalidArgument);
    }
    Ok(())
}

fn read_quote_cfg(ai: &AccountInfo, quote_mint: &Pubkey) -> Result<QuoteCfg, ProgramError> {
    if ai.key != &quote_pda(quote_mint).0 {
        return Err(ProgramError::InvalidSeeds);
    }
    if ai.owner != &id() {
        return Err(ProgramError::IllegalOwner);
    }
    QuoteCfg::read(&ai.try_borrow_data()?)
}

fn transfer_lamports<'a>(from: &AccountInfo<'a>, to: &AccountInfo<'a>, system: &AccountInfo<'a>, lamports: u64, seeds: &[&[u8]]) -> ProgramResult {
    if lamports == 0 {
        return Ok(());
    }
    let ix = system_instruction::transfer(from.key, to.key, lamports);
    if seeds.is_empty() {
        invoke(&ix, &[from.clone(), to.clone(), system.clone()])
    } else {
        invoke_signed(&ix, &[from.clone(), to.clone(), system.clone()], &[seeds])
    }
}

/// Close a wSOL account owned by a PDA: the quote amount goes to the owner
/// (to be paid out), the account's rent goes to the keeper, who pays to
/// create it again next crank. Returns the quote amount.
fn close_refund<'a>(token_program: &Pubkey, ata: &AccountInfo<'a>, owner: &AccountInfo<'a>, keeper: &AccountInfo<'a>, system: &AccountInfo<'a>, all: &[AccountInfo<'a>], seeds: &[&[u8]]) -> Result<u64, ProgramError> {
    let (_, _, amount) = tokenix::read_token_account(&ata.try_borrow_data()?)?;
    let rent = ata.lamports().saturating_sub(amount);
    invoke_signed(&tokenix::close_account(token_program, ata.key, owner.key, owner.key), all, &[seeds])?;
    transfer_lamports(owner, keeper, system, rent, seeds)?;
    Ok(amount)
}

/// Once. `ll_signer` becomes the Raydium platform's admin and every wallet,
/// so the only way to change the platform's fees is through this program.
fn create_raydium_platform(accounts: &[AccountInfo], fund_lamports: u64) -> ProgramResult {
    let it = &mut accounts.iter();
    let admin = next_account_info(it)?;
    let platform_ai = next_account_info(it)?;
    let signer_ai = next_account_info(it)?;
    let ray_cfg = next_account_info(it)?;
    let cpswap_cfg = next_account_info(it)?;
    let system = next_account_info(it)?;
    let launchlab = next_account_info(it)?;

    let platform = read_platform(platform_ai)?;
    require_admin(admin, &platform)?;
    if platform.raydium_platform_config != Pubkey::default() {
        return Err(ProgramError::AccountAlreadyInitialized);
    }
    let (signer_key, signer_bump) = signer_pda();
    expect(signer_ai, &signer_key, "ll_signer")?;
    expect(ray_cfg, &raydium::platform_config(&signer_key), "raydium platform config")?;
    expect(system, &system_program::id(), "system")?;
    expect(launchlab, &LAUNCHLAB_PROGRAM, "launchlab")?;

    transfer_lamports(admin, signer_ai, system, fund_lamports, &[])?;
    let ix = raydium::create_platform_config_ix(
        &signer_key,
        cpswap_cfg.key,
        &raydium::PlatformParams {
            platform_scale: PLATFORM_SCALE,
            creator_scale: CREATOR_SCALE,
            burn_scale: BURN_SCALE,
            fee_rate: PLATFORM_FEE_RATE,
            name: "Charlie",
            web: "https://charlieprotocol.xyz",
            img: "",
            creator_fee_rate: CREATOR_FEE_RATE,
        },
    );
    invoke_signed(
        &ix,
        &[signer_ai.clone(), ray_cfg.clone(), cpswap_cfg.clone(), system.clone(), launchlab.clone()],
        &[&[crate::SEED_SIGNER, &[signer_bump]]],
    )?;
    Platform { raydium_platform_config: *ray_cfg.key, ..platform }.write(&mut platform_ai.try_borrow_mut_data()?)?;
    msg!("raydium platform created");
    Ok(())
}

/// Launch a coin on Charlie's Raydium platform, with `ll_collect(mint)` as
/// its creator. Accounts:
///  0 `[s, w]` dev (payer, coin admin)      11 `[w]` base vault
///  1 `[]` ll_platform                     12 `[w]` quote vault
///  2 `[]` quote_cfg                       13 `[]` Token-2022 program
///  3 `[w]` ll_route (new)                 14 `[]` quote token program
///  4 `[w]` ll_collect                     15 `[]` system
///  5 `[]` LaunchLab global config          16 `[]` LaunchLab event authority
///  6 `[]` Raydium platform config          17 `[]` LaunchLab program
///  7 `[]` LaunchLab vault authority        18 `[w]` ll_collect quote ATA
///  8 `[w]` pool state                     19 `[w]` ll_collect coin ATA
///  9 `[s, w]` coin mint (new)              20 `[]` ATA program
/// 10 `[]` quote mint
fn launch(accounts: &[AccountInfo], a: LaunchArgs) -> ProgramResult {
    let ai: Vec<&AccountInfo> = accounts.iter().collect();
    if ai.len() < 21 {
        return Err(ProgramError::NotEnoughAccountKeys);
    }
    let (dev, platform_ai, quote_ai, route_ai, collect_ai) = (ai[0], ai[1], ai[2], ai[3], ai[4]);
    let (global_cfg, ray_cfg, pool, base_mint, quote_mint) = (ai[5], ai[6], ai[8], ai[9], ai[10]);
    let (token22, quote_tp, system, launchlab) = (ai[13], ai[14], ai[15], ai[17]);
    let (collect_quote_ata, collect_base_ata, ata_prog) = (ai[18], ai[19], ai[20]);

    if !dev.is_signer || !base_mint.is_signer {
        return Err(ProgramError::MissingRequiredSignature);
    }
    let platform = read_platform(platform_ai)?;
    if platform.paused {
        msg!("launch: platform paused");
        return Err(ProgramError::InvalidAccountData);
    }
    if platform.raydium_platform_config == Pubkey::default() {
        return not_built(2);
    }
    expect(ray_cfg, &platform.raydium_platform_config, "raydium platform config")?;
    let cfg = read_quote_cfg(quote_ai, quote_mint.key)?;
    if !cfg.enabled {
        msg!("launch: quote not enabled");
        return Err(ProgramError::InvalidArgument);
    }
    expect(quote_tp, &cfg.token_program, "quote token program")?;
    guards::check_quote_mint(&quote_mint.try_borrow_data()?)?;
    expect(global_cfg, &raydium::global_config(quote_mint.key, 0, 0), "global config")?;
    expect(pool, &raydium::pool_state(base_mint.key, quote_mint.key), "pool")?;
    expect(token22, &TOKEN_2022_PROGRAM, "token-2022")?;
    expect(system, &system_program::id(), "system")?;
    expect(launchlab, &LAUNCHLAB_PROGRAM, "launchlab")?;
    expect(ata_prog, &tokenix::ATA_PROGRAM, "ata program")?;
    let (collect_key, collect_bump) = collect_pda(base_mint.key);
    expect(collect_ai, &collect_key, "ll_collect")?;
    expect(collect_quote_ata, &tokenix::associated_token_address(&collect_key, quote_mint.key, &cfg.token_program), "collect quote ata")?;
    expect(collect_base_ata, &tokenix::associated_token_address(&collect_key, base_mint.key, &TOKEN_2022_PROGRAM), "collect coin ata")?;
    let (route_key, route_bump) = route_pda(base_mint.key);
    expect(route_ai, &route_key, "ll_route")?;
    if !route_ai.data_is_empty() {
        return Err(ProgramError::AccountAlreadyInitialized);
    }
    if a.fund_collect < MIN_COLLECT_FUND {
        msg!("launch: fund_collect below minimum");
        return Err(ProgramError::InvalidArgument);
    }
    let route = Route {
        mint: *base_mint.key,
        quote_mint: *quote_mint.key,
        admin: *dev.key,
        sol_burn_bps: a.sol_burn_bps,
        own_burn_bps: a.own_burn_bps,
        ops_bps: a.ops_bps,
        ops_address: a.ops_address,
        launchlab_pool: *pool.key,
        cpmm_pool: Pubkey::default(),
        coin_burned: 0,
        lamports_burned: 0,
        ops_paid: 0,
        bump: route_bump,
        collect_bump,
    };
    route.validate()?;

    transfer_lamports(dev, collect_ai, system, a.fund_collect, &[])?;
    let ix = raydium::initialize_ix(
        dev.key,
        &collect_key,
        global_cfg.key,
        ray_cfg.key,
        base_mint.key,
        quote_mint.key,
        &cfg.token_program,
        &raydium::LaunchParams {
            decimals: a.decimals,
            name: &a.name,
            symbol: &a.symbol,
            uri: &a.uri,
            supply: a.supply,
            total_base_sell: a.total_base_sell,
            total_quote_fund_raising: a.total_quote_fund_raising,
        },
    );
    let infos: Vec<AccountInfo> = ai[..18].iter().map(|x| (*x).clone()).collect();
    invoke(&ix, &infos)?;

    create_pda(dev, route_ai, system, &[crate::SEED_ROUTE, base_mint.key.as_ref(), &[route_bump]], Route::LEN)?;
    route.write(&mut route_ai.try_borrow_mut_data()?)?;

    for (ata, mint, tp) in [(collect_quote_ata, quote_mint, quote_tp), (collect_base_ata, base_mint, token22)] {
        invoke(
            &tokenix::create_ata_idempotent(dev.key, &collect_key, mint.key, tp.key),
            &[dev.clone(), ata.clone(), collect_ai.clone(), mint.clone(), system.clone(), tp.clone(), ata_prog.clone()],
        )?;
    }
    sol_log_data(&[b"charlie:launch", base_mint.key.as_ref(), quote_mint.key.as_ref(), collect_key.as_ref()]);
    Ok(())
}

/// Claim a coin's curve creator fee into `ll_collect` and route all of it,
/// in one transaction. SOL quote only in this build; a stock quote needs
/// the oracle-bounded swap of build step 5 and is refused until then.
///
/// Keeper-only for now (the platform admin): the own-burn leg buys the coin
/// in its own pool, and without an on-chain bound on that price a
/// permissionless caller could sandwich it. LAUNCHLAB-RAIL.md records this.
///
/// Accounts:
///  0 `[s, w]` keeper                      13 `[]` LaunchLab vault authority
///  1 `[]` ll_platform                     14 `[]` LaunchLab global config
///  2 `[]` quote_cfg                       15 `[]` Raydium platform config
///  3 `[w]` ll_route                       16 `[w]` pool state
///  4 `[w]` ll_collect                     17 `[w]` ll_collect coin ATA
///  5 `[]` creator fee vault authority      18 `[w]` base vault
///  6 `[w]` creator fee vault               19 `[w]` quote vault
///  7 `[w]` ll_collect quote ATA            20 `[w]` coin mint
///  8 `[]` quote mint                      21 `[]` Token-2022 program
///  9 `[]` quote token program             22 `[]` LaunchLab event authority
/// 10 `[]` system                          23 `[w]` platform fee vault
/// 11 `[]` ATA program                     24 `[w]` incinerator
/// 12 `[]` LaunchLab program               25 `[w]` ops address
fn crank_curve(accounts: &[AccountInfo]) -> ProgramResult {
    let ai: Vec<&AccountInfo> = accounts.iter().collect();
    if ai.len() < 26 {
        return Err(ProgramError::NotEnoughAccountKeys);
    }
    let (keeper, platform_ai, quote_ai, route_ai, collect_ai) = (ai[0], ai[1], ai[2], ai[3], ai[4]);
    let (fee_vault, collect_quote_ata, quote_mint, quote_tp, system) = (ai[6], ai[7], ai[8], ai[9], ai[10]);
    let (launchlab, global_cfg, ray_cfg, pool) = (ai[12], ai[14], ai[15], ai[16]);
    let (collect_base_ata, base_mint, token22, platform_vault) = (ai[17], ai[20], ai[21], ai[23]);
    let (incinerator, ops) = (ai[24], ai[25]);

    let platform = read_platform(platform_ai)?;
    require_admin(keeper, &platform)?;
    if platform.paused {
        msg!("crank: platform paused");
        return Err(ProgramError::InvalidAccountData);
    }
    if route_ai.owner != &id() {
        return Err(ProgramError::IllegalOwner);
    }
    let mut route = Route::read(&route_ai.try_borrow_data()?)?;
    expect(route_ai, &route_pda(&route.mint).0, "ll_route")?;
    expect(base_mint, &route.mint, "coin mint")?;
    expect(quote_mint, &route.quote_mint, "quote mint")?;
    let cfg = read_quote_cfg(quote_ai, quote_mint.key)?;
    if !cfg.is_sol() {
        return not_built(5);
    }
    let collect_key = *collect_ai.key;
    expect(collect_ai, &collect_pda(&route.mint).0, "ll_collect")?;
    expect(quote_tp, &cfg.token_program, "quote token program")?;
    expect(system, &system_program::id(), "system")?;
    expect(launchlab, &LAUNCHLAB_PROGRAM, "launchlab")?;
    expect(token22, &TOKEN_2022_PROGRAM, "token-2022")?;
    expect(ray_cfg, &platform.raydium_platform_config, "raydium platform config")?;
    expect(global_cfg, &raydium::global_config(quote_mint.key, 0, 0), "global config")?;
    expect(pool, &route.launchlab_pool, "pool")?;
    expect(fee_vault, &raydium::creator_fee_vault(&collect_key, quote_mint.key), "creator fee vault")?;
    expect(platform_vault, &raydium::platform_fee_vault(ray_cfg.key, quote_mint.key), "platform fee vault")?;
    expect(collect_quote_ata, &tokenix::associated_token_address(&collect_key, quote_mint.key, &cfg.token_program), "collect quote ata")?;
    expect(collect_base_ata, &tokenix::associated_token_address(&collect_key, base_mint.key, &TOKEN_2022_PROGRAM), "collect coin ata")?;
    expect(incinerator, &INCINERATOR, "incinerator")?;
    expect(ops, &route.ops_address, "ops")?;

    let seeds: &[&[u8]] = &[crate::SEED_COLLECT, route.mint.as_ref(), &[route.collect_bump]];
    let all: Vec<AccountInfo> = ai.iter().map(|x| (*x).clone()).collect();

    // 1. Claim. Recreates the quote ATA if the last crank closed it; the rent
    //    comes out of ll_collect's own lamports and comes back at step 4.
    invoke_signed(&raydium::claim_creator_fee_ix(&collect_key, collect_quote_ata.key, quote_mint.key, quote_tp.key), &all, &[seeds])?;

    // 2. Route the whole balance: this claim plus any remainder left last time.
    let (_, owner, q) = tokenix::read_token_account(&collect_quote_ata.try_borrow_data()?)?;
    if owner != collect_key {
        return Err(ProgramError::IllegalOwner);
    }
    if q < cfg.min_crank {
        msg!("crank: {} below min_crank", q);
        return Err(ProgramError::InsufficientFunds);
    }
    let legs = split_legs(q, &route);

    // 3. Own-token burn: buy the coin with its share, then burn every coin
    //    ll_collect holds.
    if legs.own_burn > 0 {
        let buy = raydium::buy_exact_in_ix(
            &collect_key, global_cfg.key, ray_cfg.key, base_mint.key, quote_mint.key,
            collect_base_ata.key, collect_quote_ata.key, quote_tp.key, &collect_key, legs.own_burn, 0,
        );
        invoke_signed(&buy, &all, &[seeds])?;
    }
    let (_, _, coins) = tokenix::read_token_account(&collect_base_ata.try_borrow_data()?)?;
    if coins > 0 {
        invoke_signed(&tokenix::burn(&TOKEN_2022_PROGRAM, collect_base_ata.key, base_mint.key, &collect_key, coins), &all, &[seeds])?;
    }

    // 4. Unwrap what is left (sol burn + ops + remainder, and the ATA's rent)
    //    into ll_collect, then pay the two SOL legs out of it.
    invoke_signed(&tokenix::close_account(quote_tp.key, collect_quote_ata.key, &collect_key, &collect_key), &all, &[seeds])?;
    transfer_lamports(collect_ai, incinerator, system, legs.sol_burn, seeds)?;
    transfer_lamports(collect_ai, ops, system, legs.ops, seeds)?;

    route.coin_burned = route.coin_burned.saturating_add(coins);
    route.lamports_burned = route.lamports_burned.saturating_add(legs.sol_burn);
    route.ops_paid = route.ops_paid.saturating_add(legs.ops);
    route.write(&mut route_ai.try_borrow_mut_data()?)?;
    sol_log_data(&[
        b"charlie:crank_curve",
        route.mint.as_ref(),
        &q.to_le_bytes(),
        &coins.to_le_bytes(),
        &legs.sol_burn.to_le_bytes(),
        &legs.ops.to_le_bytes(),
    ]);
    Ok(())
}

// -- after graduation --------------------------------------------------------

/// The coin's CPMM pool, checked: owned by CPMM, `pool_creator` is this
/// coin's `ll_collect` (only LaunchLab's permissioned migration can create a
/// pool with that creator), and its two mints are the coin and its quote.
/// Recorded in `ll_route` on first use and required to match after.
fn load_pool(pool_ai: &AccountInfo, route: &mut Route, collect_key: &Pubkey) -> Result<cpmm::Pool, ProgramError> {
    if pool_ai.owner != &CPMM_PROGRAM {
        return Err(ProgramError::IllegalOwner);
    }
    let p = cpmm::read_pool(&pool_ai.try_borrow_data()?)?;
    if &p.pool_creator != collect_key {
        msg!("pool: creator is not this coin's ll_collect");
        return Err(ProgramError::InvalidArgument);
    }
    let pair = (p.mint0 == route.mint && p.mint1 == route.quote_mint) || (p.mint1 == route.mint && p.mint0 == route.quote_mint);
    if !pair {
        msg!("pool: not this coin's pair");
        return Err(ProgramError::InvalidArgument);
    }
    if route.cpmm_pool == Pubkey::default() {
        route.cpmm_pool = *pool_ai.key;
    } else if &route.cpmm_pool != pool_ai.key {
        return Err(ProgramError::InvalidArgument);
    }
    Ok(p)
}

fn token_amount(ai: &AccountInfo, owner: &Pubkey, mint: &Pubkey) -> Result<u64, ProgramError> {
    let (m, o, amt) = tokenix::read_token_account(&ai.try_borrow_data()?)?;
    if &o != owner || &m != mint {
        return Err(ProgramError::InvalidAccountData);
    }
    Ok(amt)
}

/// Accounts `route_cpmm` needs, all already checked by its caller.
struct CpmmLegs<'a, 'b> {
    all: &'b [AccountInfo<'a>],
    keeper: &'b AccountInfo<'a>,
    collect: &'b AccountInfo<'a>,
    collect_quote: &'b AccountInfo<'a>,
    collect_coin: &'b AccountInfo<'a>,
    coin_mint: &'b AccountInfo<'a>,
    coin_program: Pubkey,
    quote_program: Pubkey,
    incinerator: &'b AccountInfo<'a>,
    ops: &'b AccountInfo<'a>,
    system: &'b AccountInfo<'a>,
}

/// Route whatever quote and coin `ll_collect` holds through the coin's legs,
/// buying on the graduated CPMM pool. Below `min_crank` the quote side is
/// left for next time and only coins are burned.
fn route_cpmm(l: &CpmmLegs, route: &mut Route, cfg: &QuoteCfg, pool_key: &Pubkey, p: &cpmm::Pool) -> Result<(u64, u64, crate::state::Legs), ProgramError> {
    let collect_key = *l.collect.key;
    let seeds: &[&[u8]] = &[crate::SEED_COLLECT, route.mint.as_ref(), &[route.collect_bump]];
    let q = token_amount(l.collect_quote, &collect_key, &route.quote_mint)?;
    let mut legs = crate::state::Legs { sol_burn: 0, own_burn: 0, ops: 0, remainder: q };
    let route_quote = q >= cfg.min_crank && q > 0;
    if route_quote {
        legs = split_legs(q, route);
        if legs.own_burn > 0 {
            let quote_is_0 = p.mint0 == route.quote_mint;
            invoke_signed(
                &cpmm::buy_coin_ix(&collect_key, pool_key, p, quote_is_0, l.collect_quote.key, l.collect_coin.key, legs.own_burn, 0),
                l.all,
                &[seeds],
            )?;
        }
    }
    let coins = token_amount(l.collect_coin, &collect_key, &route.mint)?;
    if coins > 0 {
        invoke_signed(&tokenix::burn(&l.coin_program, l.collect_coin.key, l.coin_mint.key, &collect_key, coins), l.all, &[seeds])?;
    }
    if route_quote {
        close_refund(&l.quote_program, l.collect_quote, l.collect, l.keeper, l.system, l.all, seeds)?;
        transfer_lamports(l.collect, l.incinerator, l.system, legs.sol_burn, seeds)?;
        transfer_lamports(l.collect, l.ops, l.system, legs.ops, seeds)?;
    }
    route.coin_burned = route.coin_burned.saturating_add(coins);
    route.lamports_burned = route.lamports_burned.saturating_add(legs.sol_burn);
    route.ops_paid = route.ops_paid.saturating_add(legs.ops);
    Ok((q, coins, legs))
}

/// After graduation: collect the pool's creator fee (both tokens) into
/// `ll_collect` with CPMM's permissionless collect, then route it.
/// Keeper-only for the same reason as `crank_curve`. Accounts:
///  0 `[s, w]` keeper                      11 `[w]` pool mint 1
///  1 `[]` ll_platform                     12 `[]` token program of mint 0
///  2 `[]` quote_cfg                       13 `[]` token program of mint 1
///  3 `[w]` ll_route                       14 `[w]` ll_collect ATA for mint 0
///  4 `[w]` ll_collect                     15 `[w]` ll_collect ATA for mint 1
///  5 `[]` CPMM program                    16 `[]` ATA program
///  6 `[]` CPMM authority                  17 `[]` system
///  7 `[w]` CPMM pool                      18 `[]` CPMM amm config
///  8 `[w]` pool vault 0                   19 `[w]` CPMM observation
///  9 `[w]` pool vault 1                   20 `[w]` incinerator
/// 10 `[w]` pool mint 0                    21 `[w]` ops address
///                                        22 `[]` CPMM creator_fee_share PDA
fn crank_amm_creator(accounts: &[AccountInfo]) -> ProgramResult {
    let ai: Vec<&AccountInfo> = accounts.iter().collect();
    if ai.len() < 23 {
        return Err(ProgramError::NotEnoughAccountKeys);
    }
    let (keeper, platform_ai, quote_ai, route_ai, collect_ai) = (ai[0], ai[1], ai[2], ai[3], ai[4]);
    let platform = read_platform(platform_ai)?;
    require_admin(keeper, &platform)?;
    if platform.paused {
        return Err(ProgramError::InvalidAccountData);
    }
    if route_ai.owner != &id() {
        return Err(ProgramError::IllegalOwner);
    }
    let mut route = Route::read(&route_ai.try_borrow_data()?)?;
    expect(route_ai, &route_pda(&route.mint).0, "ll_route")?;
    expect(collect_ai, &collect_pda(&route.mint).0, "ll_collect")?;
    let cfg = read_quote_cfg(quote_ai, &route.quote_mint)?;
    if !cfg.is_sol() {
        return not_built(5);
    }
    let p = load_pool(ai[7], &mut route, collect_ai.key)?;
    expect(ai[5], &CPMM_PROGRAM, "cpmm")?;
    expect(ai[6], &cpmm::authority(), "cpmm authority")?;
    expect(ai[8], &p.vault0, "vault 0")?;
    expect(ai[9], &p.vault1, "vault 1")?;
    expect(ai[10], &p.mint0, "mint 0")?;
    expect(ai[11], &p.mint1, "mint 1")?;
    expect(ai[12], &p.prog0, "program 0")?;
    expect(ai[13], &p.prog1, "program 1")?;
    let ata0 = tokenix::associated_token_address(collect_ai.key, &p.mint0, &p.prog0);
    let ata1 = tokenix::associated_token_address(collect_ai.key, &p.mint1, &p.prog1);
    expect(ai[14], &ata0, "collect ata 0")?;
    expect(ai[15], &ata1, "collect ata 1")?;
    expect(ai[16], &tokenix::ATA_PROGRAM, "ata program")?;
    expect(ai[17], &system_program::id(), "system")?;
    expect(ai[18], &p.amm_config, "amm config")?;
    expect(ai[19], &p.observation, "observation")?;
    expect(ai[20], &INCINERATOR, "incinerator")?;
    expect(ai[21], &route.ops_address, "ops")?;
    expect(ai[22], &cpmm::creator_fee_share(collect_ai.key, &p.amm_config), "creator fee share")?;

    let all: Vec<AccountInfo> = ai.iter().map(|x| (*x).clone()).collect();
    invoke(&cpmm::collect_creator_fee_ix(keeper.key, collect_ai.key, ai[7].key, &p, &ata0, &ata1), &all)?;

    let quote_is_0 = p.mint0 == route.quote_mint;
    let (qi, ci) = if quote_is_0 { (14, 15) } else { (15, 14) };
    let l = CpmmLegs {
        all: &all,
        keeper,
        collect: collect_ai,
        collect_quote: ai[qi],
        collect_coin: ai[ci],
        coin_mint: if quote_is_0 { ai[11] } else { ai[10] },
        coin_program: if quote_is_0 { p.prog1 } else { p.prog0 },
        quote_program: if quote_is_0 { p.prog0 } else { p.prog1 },
        incinerator: ai[20],
        ops: ai[21],
        system: ai[17],
    };
    let (q, coins, legs) = route_cpmm(&l, &mut route, &cfg, ai[7].key, &p)?;
    route.write(&mut route_ai.try_borrow_mut_data()?)?;
    sol_log_data(&[b"charlie:crank_amm_creator", route.mint.as_ref(), &q.to_le_bytes(), &coins.to_le_bytes(), &legs.sol_burn.to_le_bytes(), &legs.ops.to_le_bytes()]);
    Ok(())
}

/// After graduation: `ll_signer` claims the pool's LP fees with its Fee Key.
/// Coin-side fees are burned. Quote-side: `LP_CHARLIE_BPS` unwrapped and paid
/// to `charlie_pool`, the rest moved to `ll_collect` and routed. Accounts:
///  0 `[s, w]` keeper            11 `[]` CPMM authority       22 `[]` Token-2022 program
///  1 `[]` ll_platform           12 `[w]` CPMM pool           23 `[]` memo program
///  2 `[]` quote_cfg             13 `[w]` LP mint             24 `[]` ATA program
///  3 `[w]` ll_route             14 `[w]` ll_signer ATA mint0 25 `[]` system
///  4 `[w]` ll_collect           15 `[w]` ll_signer ATA mint1 26 `[w]` ll_collect ATA mint 0
///  5 `[w]` ll_signer            16 `[w]` pool vault 0        27 `[w]` ll_collect ATA mint 1
///  6 `[]` lock program          17 `[w]` pool vault 1        28 `[]` CPMM amm config
///  7 `[]` lock authority        18 `[w]` pool mint 0         29 `[w]` CPMM observation
///  8 `[w]` Fee Key NFT account  19 `[w]` pool mint 1         30 `[w]` incinerator
///  9 `[w]` lock position        20 `[w]` lock LP vault       31 `[w]` ops address
/// 10 `[]` CPMM program          21 `[]` SPL Token program    32 `[w]` charlie_pool
fn crank_lp(accounts: &[AccountInfo], lp_fee_amount: u64) -> ProgramResult {
    let ai: Vec<&AccountInfo> = accounts.iter().collect();
    if ai.len() < 33 {
        return Err(ProgramError::NotEnoughAccountKeys);
    }
    let (keeper, platform_ai, quote_ai, route_ai, collect_ai, signer_ai) = (ai[0], ai[1], ai[2], ai[3], ai[4], ai[5]);
    let platform = read_platform(platform_ai)?;
    require_admin(keeper, &platform)?;
    if platform.paused {
        return Err(ProgramError::InvalidAccountData);
    }
    if route_ai.owner != &id() {
        return Err(ProgramError::IllegalOwner);
    }
    let mut route = Route::read(&route_ai.try_borrow_data()?)?;
    expect(route_ai, &route_pda(&route.mint).0, "ll_route")?;
    expect(collect_ai, &collect_pda(&route.mint).0, "ll_collect")?;
    let cfg = read_quote_cfg(quote_ai, &route.quote_mint)?;
    if !cfg.is_sol() {
        return not_built(5);
    }
    let (signer_key, signer_bump) = signer_pda();
    expect(signer_ai, &signer_key, "ll_signer")?;
    let p = load_pool(ai[12], &mut route, collect_ai.key)?;
    expect(ai[6], &LOCK_PROGRAM, "lock program")?;
    expect(ai[7], &cpmm::LOCK_AUTH, "lock authority")?;
    let (nft_mint, nft_owner, nft_amt) = tokenix::read_token_account(&ai[8].try_borrow_data()?)?;
    if nft_owner != signer_key || nft_amt != 1 || ai[8].owner != &TOKEN_PROGRAM {
        msg!("crank_lp: not ll_signer's Fee Key");
        return Err(ProgramError::InvalidAccountData);
    }
    expect(ai[9], &cpmm::lock_position(&nft_mint), "lock position")?;
    if ai[9].owner != &LOCK_PROGRAM {
        return Err(ProgramError::IllegalOwner);
    }
    let (lock_pool, lock_nft, lock_lp) = cpmm::read_lock(&ai[9].try_borrow_data()?)?;
    if &lock_pool != ai[12].key || lock_nft != nft_mint || lock_lp != p.lp_mint {
        msg!("crank_lp: Fee Key is not this pool's");
        return Err(ProgramError::InvalidAccountData);
    }
    expect(ai[10], &CPMM_PROGRAM, "cpmm")?;
    expect(ai[11], &cpmm::authority(), "cpmm authority")?;
    expect(ai[13], &p.lp_mint, "lp mint")?;
    let s0 = tokenix::associated_token_address(&signer_key, &p.mint0, &p.prog0);
    let s1 = tokenix::associated_token_address(&signer_key, &p.mint1, &p.prog1);
    expect(ai[14], &s0, "signer ata 0")?;
    expect(ai[15], &s1, "signer ata 1")?;
    expect(ai[16], &p.vault0, "vault 0")?;
    expect(ai[17], &p.vault1, "vault 1")?;
    expect(ai[18], &p.mint0, "mint 0")?;
    expect(ai[19], &p.mint1, "mint 1")?;
    expect(ai[20], &tokenix::associated_token_address(&cpmm::LOCK_AUTH, &p.lp_mint, &TOKEN_PROGRAM), "lock lp vault")?;
    expect(ai[21], &TOKEN_PROGRAM, "token")?;
    expect(ai[22], &TOKEN_2022_PROGRAM, "token-2022")?;
    expect(ai[23], &cpmm::MEMO_PROGRAM, "memo")?;
    expect(ai[24], &tokenix::ATA_PROGRAM, "ata program")?;
    expect(ai[25], &system_program::id(), "system")?;
    let c0 = tokenix::associated_token_address(collect_ai.key, &p.mint0, &p.prog0);
    let c1 = tokenix::associated_token_address(collect_ai.key, &p.mint1, &p.prog1);
    expect(ai[26], &c0, "collect ata 0")?;
    expect(ai[27], &c1, "collect ata 1")?;
    expect(ai[28], &p.amm_config, "amm config")?;
    expect(ai[29], &p.observation, "observation")?;
    expect(ai[30], &INCINERATOR, "incinerator")?;
    expect(ai[31], &route.ops_address, "ops")?;
    expect(ai[32], &platform.charlie_pool, "charlie_pool")?;

    let all: Vec<AccountInfo> = ai.iter().map(|x| (*x).clone()).collect();
    let (system, ata_prog) = (ai[25], ai[24]);
    // Token accounts the claim and the routing need, paid for by the keeper.
    for (owner, ata, mint, prog) in [
        (signer_ai, ai[14], ai[18], &p.prog0),
        (signer_ai, ai[15], ai[19], &p.prog1),
        (collect_ai, ai[26], ai[18], &p.prog0),
        (collect_ai, ai[27], ai[19], &p.prog1),
    ] {
        let prog_ai = if prog == &TOKEN_PROGRAM { ai[21] } else { ai[22] };
        invoke(
            &tokenix::create_ata_idempotent(keeper.key, owner.key, mint.key, prog),
            &[keeper.clone(), ata.clone(), owner.clone(), mint.clone(), system.clone(), prog_ai.clone(), ata_prog.clone()],
        )?;
    }
    let quote_is_0 = p.mint0 == route.quote_mint;
    let (sq, sc) = if quote_is_0 { (ai[14], ai[15]) } else { (ai[15], ai[14]) };
    let q_before = token_amount(sq, &signer_key, &route.quote_mint)?;
    let c_before = token_amount(sc, &signer_key, &route.mint)?;

    let signer_seeds: &[&[u8]] = &[crate::SEED_SIGNER, &[signer_bump]];
    invoke_signed(&cpmm::collect_cp_fee_ix(&signer_key, ai[8].key, &nft_mint, ai[12].key, &p, &s0, &s1, lp_fee_amount), &all, &[signer_seeds])?;

    let dq = token_amount(sq, &signer_key, &route.quote_mint)?.saturating_sub(q_before);
    let dc = token_amount(sc, &signer_key, &route.mint)?.saturating_sub(c_before);
    let (coin_prog, quote_prog) = if quote_is_0 { (p.prog1, p.prog0) } else { (p.prog0, p.prog1) };
    let coin_mint_ai = if quote_is_0 { ai[19] } else { ai[18] };
    if dc > 0 {
        invoke_signed(&tokenix::burn(&coin_prog, sc.key, coin_mint_ai.key, &signer_key, dc), &all, &[signer_seeds])?;
    }
    let charlie = (dq as u128 * LP_CHARLIE_BPS as u128 / 10_000) as u64;
    let dev = dq - charlie;
    let (cq, cc) = if quote_is_0 { (ai[26], ai[27]) } else { (ai[27], ai[26]) };
    if dev > 0 {
        invoke_signed(&tokenix::transfer(&quote_prog, sq.key, cq.key, &signer_key, dev), &all, &[signer_seeds])?;
    }
    // Unwrap ll_signer's quote account (Charlie's share plus rent) and pay
    // Charlie's share, and only that, to charlie_pool.
    close_refund(&quote_prog, sq, signer_ai, keeper, system, &all, signer_seeds)?;
    transfer_lamports(signer_ai, ai[32], system, charlie, signer_seeds)?;

    let l = CpmmLegs {
        all: &all,
        keeper,
        collect: collect_ai,
        collect_quote: cq,
        collect_coin: cc,
        coin_mint: coin_mint_ai,
        coin_program: coin_prog,
        quote_program: quote_prog,
        incinerator: ai[30],
        ops: ai[31],
        system,
    };
    let (q, coins, legs) = route_cpmm(&l, &mut route, &cfg, ai[12].key, &p)?;
    route.coin_burned = route.coin_burned.saturating_add(dc);
    route.write(&mut route_ai.try_borrow_mut_data()?)?;
    sol_log_data(&[b"charlie:crank_lp", route.mint.as_ref(), &dq.to_le_bytes(), &dc.to_le_bytes(), &charlie.to_le_bytes(), &q.to_le_bytes(), &coins.to_le_bytes(), &legs.sol_burn.to_le_bytes(), &legs.ops.to_le_bytes()]);
    Ok(())
}

/// The curve's platform fee (0.25%, every coin on the rail): `ll_signer`
/// claims it from LaunchLab's platform fee vault, unwraps it, and pays all of
/// it to `charlie_pool`, the Charlie leg. Keeper-only like the other cranks.
/// Accounts:
///  0 `[s, w]` keeper                  7 `[w]` LaunchLab platform fee vault
///  1 `[]` ll_platform                 8 `[w]` ll_signer quote ATA
///  2 `[]` quote_cfg                   9 `[]` quote mint
///  3 `[w]` ll_signer                 10 `[]` quote token program
///  4 `[]` LaunchLab program          11 `[]` system
///  5 `[]` platform fee vault auth    12 `[]` ATA program
///  6 `[]` Raydium platform config    13 `[w]` charlie_pool
fn crank_platform(accounts: &[AccountInfo]) -> ProgramResult {
    let ai: Vec<&AccountInfo> = accounts.iter().collect();
    if ai.len() < 14 {
        return Err(ProgramError::NotEnoughAccountKeys);
    }
    let (keeper, platform_ai, quote_ai, signer_ai) = (ai[0], ai[1], ai[2], ai[3]);
    let platform = read_platform(platform_ai)?;
    require_admin(keeper, &platform)?;
    if platform.paused {
        return Err(ProgramError::InvalidAccountData);
    }
    let quote_mint = *ai[9].key;
    let cfg = read_quote_cfg(quote_ai, &quote_mint)?;
    if !cfg.is_sol() {
        return not_built(5);
    }
    let (signer_key, signer_bump) = signer_pda();
    expect(signer_ai, &signer_key, "ll_signer")?;
    expect(ai[4], &LAUNCHLAB_PROGRAM, "launchlab")?;
    expect(ai[5], &raydium::platform_fee_vault_authority(), "platform fee vault auth")?;
    expect(ai[6], &platform.raydium_platform_config, "raydium platform config")?;
    expect(ai[7], &raydium::platform_fee_vault(ai[6].key, &quote_mint), "platform fee vault")?;
    expect(ai[10], &TOKEN_PROGRAM, "token program")?;
    expect(ai[8], &tokenix::associated_token_address(&signer_key, &quote_mint, &TOKEN_PROGRAM), "signer quote ata")?;
    expect(ai[11], &system_program::id(), "system")?;
    expect(ai[12], &tokenix::ATA_PROGRAM, "ata program")?;
    expect(ai[13], &platform.charlie_pool, "charlie_pool")?;
    let (_, _, vault_amt) = tokenix::read_token_account(&ai[7].try_borrow_data()?)?;
    if vault_amt < cfg.min_crank {
        msg!("crank_platform: below min_crank");
        return Err(ProgramError::InsufficientFunds);
    }

    let all: Vec<AccountInfo> = ai.iter().map(|x| (*x).clone()).collect();
    let seeds: &[&[u8]] = &[crate::SEED_SIGNER, &[signer_bump]];
    // ll_signer pays the ATA's rent if LaunchLab has to create it and gets
    // it back on close, so only the claimed quote leaves ll_signer.
    invoke_signed(&raydium::claim_platform_fee_ix(&signer_key, ai[6].key, ai[8].key, &quote_mint, &TOKEN_PROGRAM), &all, &[seeds])?;
    let (_, _, q) = tokenix::read_token_account(&ai[8].try_borrow_data()?)?;
    invoke_signed(&tokenix::close_account(&TOKEN_PROGRAM, ai[8].key, &signer_key, &signer_key), &all, &[seeds])?;
    transfer_lamports(signer_ai, ai[13], ai[11], q, seeds)?;
    sol_log_data(&[b"charlie:crank_platform", quote_mint.as_ref(), &q.to_le_bytes()]);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn upgrade_authority_layout() {
        let auth = Pubkey::new_from_array([7; 32]);
        let mut d = vec![0u8; 45];
        d[0..4].copy_from_slice(&3u32.to_le_bytes());
        d[12] = 1;
        d[13..45].copy_from_slice(auth.as_ref());
        assert_eq!(upgrade_authority(&d).unwrap(), Some(auth));
        d[12] = 0;
        assert_eq!(upgrade_authority(&d).unwrap(), None);
        d[0] = 2;
        assert!(upgrade_authority(&d).is_err(), "not a ProgramData account");
    }
}

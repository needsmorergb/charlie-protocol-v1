//! Raydium LaunchLab: the addresses this program derives and the
//! instructions it builds. Every layout here was read from LaunchLab's
//! on-chain Anchor IDL (v0.2.0) on mainnet and on devnet, 18 September 2026,
//! and the buy's trailing accounts from `raydium-sdk-V2` at `c289783`.
//!
//! Nothing here takes an address from a caller. The processor passes the
//! accounts it was given; these functions say what each one must be.

use solana_program::{
    instruction::{AccountMeta, Instruction},
    pubkey::Pubkey,
};

use crate::{LAUNCHLAB_PROGRAM, TOKEN_2022_PROGRAM};

pub const DISC_INITIALIZE_WITH_TOKEN_2022: [u8; 8] = [37, 190, 126, 222, 44, 154, 171, 17];
pub const DISC_CLAIM_CREATOR_FEE: [u8; 8] = [26, 97, 138, 203, 132, 171, 141, 252];
pub const DISC_CLAIM_PLATFORM_FEE_FROM_VAULT: [u8; 8] = [117, 241, 198, 168, 248, 218, 80, 29];
pub const DISC_BUY_EXACT_IN: [u8; 8] = [250, 234, 13, 123, 213, 156, 19, 236];
pub const DISC_CREATE_PLATFORM_CONFIG: [u8; 8] = [176, 90, 196, 175, 253, 113, 220, 20];

pub fn vault_authority() -> Pubkey {
    Pubkey::find_program_address(&[b"vault_auth_seed"], &LAUNCHLAB_PROGRAM).0
}
pub fn event_authority() -> Pubkey {
    Pubkey::find_program_address(&[b"__event_authority"], &LAUNCHLAB_PROGRAM).0
}
pub fn platform_config(platform_admin: &Pubkey) -> Pubkey {
    Pubkey::find_program_address(&[b"platform_config", platform_admin.as_ref()], &LAUNCHLAB_PROGRAM).0
}
pub fn pool_state(base_mint: &Pubkey, quote_mint: &Pubkey) -> Pubkey {
    Pubkey::find_program_address(&[b"pool", base_mint.as_ref(), quote_mint.as_ref()], &LAUNCHLAB_PROGRAM).0
}
pub fn pool_vault(pool: &Pubkey, mint: &Pubkey) -> Pubkey {
    Pubkey::find_program_address(&[b"pool_vault", pool.as_ref(), mint.as_ref()], &LAUNCHLAB_PROGRAM).0
}
pub fn creator_fee_vault_authority() -> Pubkey {
    Pubkey::find_program_address(&[b"creator_fee_vault_auth_seed"], &LAUNCHLAB_PROGRAM).0
}
/// Seeds are `[creator, quote_mint]` with no prefix. Because every coin's
/// creator is its own `ll_collect(mint)`, this vault is per coin.
pub fn creator_fee_vault(creator: &Pubkey, quote_mint: &Pubkey) -> Pubkey {
    Pubkey::find_program_address(&[creator.as_ref(), quote_mint.as_ref()], &LAUNCHLAB_PROGRAM).0
}
pub fn platform_fee_vault(platform_config: &Pubkey, quote_mint: &Pubkey) -> Pubkey {
    Pubkey::find_program_address(&[platform_config.as_ref(), quote_mint.as_ref()], &LAUNCHLAB_PROGRAM).0
}
/// A LaunchLab global config: `[global_config, quote_mint, curve_type u8, index u16 LE]`.
pub fn global_config(quote_mint: &Pubkey, curve_type: u8, index: u16) -> Pubkey {
    Pubkey::find_program_address(
        &[b"global_config", quote_mint.as_ref(), &[curve_type], &index.to_le_bytes()],
        &LAUNCHLAB_PROGRAM,
    )
    .0
}

fn borsh_str(v: &mut Vec<u8>, s: &str) {
    v.extend_from_slice(&(s.len() as u32).to_le_bytes());
    v.extend_from_slice(s.as_bytes());
}

pub struct PlatformParams<'a> {
    pub platform_scale: u64,
    pub creator_scale: u64,
    pub burn_scale: u64,
    pub fee_rate: u64,
    pub name: &'a str,
    pub web: &'a str,
    pub img: &'a str,
    pub creator_fee_rate: u64,
}

/// `create_platform_config`. `admin` signs and pays; for this program it is
/// `ll_signer`, and it is also the fee wallet, NFT wallet, transfer-fee
/// extension authority and vesting wallet.
pub fn create_platform_config_ix(admin: &Pubkey, cpswap_config: &Pubkey, p: &PlatformParams) -> Instruction {
    let mut data = DISC_CREATE_PLATFORM_CONFIG.to_vec();
    // PlatformParams { migrate_nft_info { platform, creator, burn }, fee_rate,
    // name, web, img, creator_fee_rate, platform_vesting_scale }
    data.extend_from_slice(&p.platform_scale.to_le_bytes());
    data.extend_from_slice(&p.creator_scale.to_le_bytes());
    data.extend_from_slice(&p.burn_scale.to_le_bytes());
    data.extend_from_slice(&p.fee_rate.to_le_bytes());
    borsh_str(&mut data, p.name);
    borsh_str(&mut data, p.web);
    borsh_str(&mut data, p.img);
    data.extend_from_slice(&p.creator_fee_rate.to_le_bytes());
    data.extend_from_slice(&0u64.to_le_bytes());
    Instruction {
        program_id: LAUNCHLAB_PROGRAM,
        accounts: vec![
            AccountMeta::new(*admin, true),
            AccountMeta::new_readonly(*admin, false),
            AccountMeta::new_readonly(*admin, false),
            AccountMeta::new(platform_config(admin), false),
            AccountMeta::new_readonly(*cpswap_config, false),
            AccountMeta::new_readonly(solana_program::system_program::id(), false),
            AccountMeta::new_readonly(*admin, false),
            AccountMeta::new_readonly(*admin, false),
        ],
        data,
    }
}

pub struct LaunchParams<'a> {
    pub decimals: u8,
    pub name: &'a str,
    pub symbol: &'a str,
    pub uri: &'a str,
    pub supply: u64,
    pub total_base_sell: u64,
    pub total_quote_fund_raising: u64,
}

/// `initialize_with_token_2022`: a constant-product curve that migrates to
/// CPMM (`migrate_type` 1), no vesting, creator fee on both tokens after
/// graduation, and no transfer-fee extension on the coin.
#[allow(clippy::too_many_arguments)]
pub fn initialize_ix(
    payer: &Pubkey,
    creator: &Pubkey,
    global_config: &Pubkey,
    platform_config: &Pubkey,
    base_mint: &Pubkey,
    quote_mint: &Pubkey,
    quote_token_program: &Pubkey,
    p: &LaunchParams,
) -> Instruction {
    let pool = pool_state(base_mint, quote_mint);
    let mut data = DISC_INITIALIZE_WITH_TOKEN_2022.to_vec();
    data.push(p.decimals);
    borsh_str(&mut data, p.name);
    borsh_str(&mut data, p.symbol);
    borsh_str(&mut data, p.uri);
    data.push(0); // CurveParams::Constant
    data.extend_from_slice(&p.supply.to_le_bytes());
    data.extend_from_slice(&p.total_base_sell.to_le_bytes());
    data.extend_from_slice(&p.total_quote_fund_raising.to_le_bytes());
    data.push(1); // migrate_type: CPMM
    data.extend_from_slice(&[0u8; 24]); // VestingParams: all zero
    data.push(1); // AmmCreatorFeeOn::BothToken
    data.push(0); // Option<TransferFeeExtensionParams>: None
    Instruction {
        program_id: LAUNCHLAB_PROGRAM,
        accounts: vec![
            AccountMeta::new(*payer, true),
            AccountMeta::new_readonly(*creator, false),
            AccountMeta::new_readonly(*global_config, false),
            AccountMeta::new_readonly(*platform_config, false),
            AccountMeta::new_readonly(vault_authority(), false),
            AccountMeta::new(pool, false),
            AccountMeta::new(*base_mint, true),
            AccountMeta::new_readonly(*quote_mint, false),
            AccountMeta::new(pool_vault(&pool, base_mint), false),
            AccountMeta::new(pool_vault(&pool, quote_mint), false),
            AccountMeta::new_readonly(TOKEN_2022_PROGRAM, false),
            AccountMeta::new_readonly(*quote_token_program, false),
            AccountMeta::new_readonly(solana_program::system_program::id(), false),
            AccountMeta::new_readonly(event_authority(), false),
            AccountMeta::new_readonly(LAUNCHLAB_PROGRAM, false),
        ],
        data,
    }
}

/// `claim_creator_fee`. `creator` signs and pays rent for `recipient` if it
/// is missing, which is why `ll_collect` is a System account with lamports.
pub fn claim_creator_fee_ix(creator: &Pubkey, recipient: &Pubkey, quote_mint: &Pubkey, quote_token_program: &Pubkey) -> Instruction {
    Instruction {
        program_id: LAUNCHLAB_PROGRAM,
        accounts: vec![
            AccountMeta::new(*creator, true),
            AccountMeta::new_readonly(creator_fee_vault_authority(), false),
            AccountMeta::new(creator_fee_vault(creator, quote_mint), false),
            AccountMeta::new(*recipient, false),
            AccountMeta::new_readonly(*quote_mint, false),
            AccountMeta::new_readonly(*quote_token_program, false),
            AccountMeta::new_readonly(solana_program::system_program::id(), false),
            AccountMeta::new_readonly(crate::tokenix::ATA_PROGRAM, false),
        ],
        data: DISC_CLAIM_CREATOR_FEE.to_vec(),
    }
}

pub fn platform_fee_vault_authority() -> Pubkey {
    Pubkey::find_program_address(&[b"platform_fee_vault_auth_seed"], &LAUNCHLAB_PROGRAM).0
}

/// `claim_platform_fee_from_vault` (`raydium-sdk-v2` 0.2.70). The platform's
/// claim-fee wallet (`ll_signer`) signs; `recipient` is its quote ATA, created
/// by LaunchLab if missing.
pub fn claim_platform_fee_ix(fee_wallet: &Pubkey, platform_config: &Pubkey, recipient: &Pubkey, quote_mint: &Pubkey, quote_token_program: &Pubkey) -> Instruction {
    Instruction {
        program_id: LAUNCHLAB_PROGRAM,
        accounts: vec![
            AccountMeta::new(*fee_wallet, true),
            AccountMeta::new_readonly(platform_fee_vault_authority(), false),
            AccountMeta::new_readonly(*platform_config, false),
            AccountMeta::new(platform_fee_vault(platform_config, quote_mint), false),
            AccountMeta::new(*recipient, false),
            AccountMeta::new_readonly(*quote_mint, false),
            AccountMeta::new_readonly(*quote_token_program, false),
            AccountMeta::new_readonly(solana_program::system_program::id(), false),
            AccountMeta::new_readonly(crate::tokenix::ATA_PROGRAM, false),
        ],
        data: DISC_CLAIM_PLATFORM_FEE_FROM_VAULT.to_vec(),
    }
}

/// `buy_exact_in` on the curve, with the three trailing accounts the SDK
/// appends (system program, platform fee vault, creator fee vault).
#[allow(clippy::too_many_arguments)]
pub fn buy_exact_in_ix(
    payer: &Pubkey,
    global_config: &Pubkey,
    platform_config: &Pubkey,
    base_mint: &Pubkey,
    quote_mint: &Pubkey,
    user_base: &Pubkey,
    user_quote: &Pubkey,
    quote_token_program: &Pubkey,
    creator: &Pubkey,
    amount_in: u64,
    minimum_amount_out: u64,
) -> Instruction {
    let pool = pool_state(base_mint, quote_mint);
    let mut data = DISC_BUY_EXACT_IN.to_vec();
    data.extend_from_slice(&amount_in.to_le_bytes());
    data.extend_from_slice(&minimum_amount_out.to_le_bytes());
    data.extend_from_slice(&0u64.to_le_bytes()); // share_fee_rate
    Instruction {
        program_id: LAUNCHLAB_PROGRAM,
        accounts: vec![
            AccountMeta::new(*payer, true),
            AccountMeta::new_readonly(vault_authority(), false),
            AccountMeta::new_readonly(*global_config, false),
            AccountMeta::new_readonly(*platform_config, false),
            AccountMeta::new(pool, false),
            AccountMeta::new(*user_base, false),
            AccountMeta::new(*user_quote, false),
            AccountMeta::new(pool_vault(&pool, base_mint), false),
            AccountMeta::new(pool_vault(&pool, quote_mint), false),
            AccountMeta::new_readonly(*base_mint, false),
            AccountMeta::new_readonly(*quote_mint, false),
            AccountMeta::new_readonly(TOKEN_2022_PROGRAM, false),
            AccountMeta::new_readonly(*quote_token_program, false),
            AccountMeta::new_readonly(event_authority(), false),
            AccountMeta::new_readonly(LAUNCHLAB_PROGRAM, false),
            AccountMeta::new_readonly(solana_program::system_program::id(), false),
            AccountMeta::new(platform_fee_vault(platform_config, quote_mint), false),
            AccountMeta::new(creator_fee_vault(creator, quote_mint), false),
        ],
        data,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn discriminators_are_anchor_sighashes() {
        use solana_program::hash::hash;
        for (name, disc) in [
            ("initialize_with_token_2022", DISC_INITIALIZE_WITH_TOKEN_2022),
            ("claim_creator_fee", DISC_CLAIM_CREATOR_FEE),
            ("claim_platform_fee_from_vault", DISC_CLAIM_PLATFORM_FEE_FROM_VAULT),
            ("buy_exact_in", DISC_BUY_EXACT_IN),
            ("create_platform_config", DISC_CREATE_PLATFORM_CONFIG),
        ] {
            let h = hash(format!("global:{name}").as_bytes());
            assert_eq!(&h.to_bytes()[..8], &disc, "{name}");
        }
    }

    #[cfg(not(feature = "devnet"))]
    #[test]
    fn mainnet_configs_match_what_was_read() {
        use solana_program::pubkey;
        let wsol = crate::WSOL_MINT;
        assert_eq!(global_config(&wsol, 0, 0), pubkey!("6s1xP3hpbAfFoNtUNF8mfHsjr2Bd97JxFJRWLbL6aHuX"));
        let spyx = pubkey!("XsoCS1TfEyfFhfvj8EtZ528L3CaKBDBRqRapnBbDF2W");
        assert_eq!(global_config(&spyx, 0, 0), pubkey!("B7ctMMdGvy46Am56myTtzfkNzt9kWZVTNGM2BWrJ9adg"));
    }

    #[cfg(feature = "devnet")]
    #[test]
    fn devnet_wsol_config_matches_what_was_read() {
        use solana_program::pubkey;
        assert_eq!(global_config(&crate::WSOL_MINT, 0, 0), pubkey!("7ZR4zD7PYfY2XxoG1Gxcy2EgEeGYrpxrwzPuwdUBssEt"));
    }

    #[test]
    fn launch_data_layout() {
        let k = Pubkey::new_unique();
        let ix = initialize_ix(&k, &k, &k, &k, &k, &crate::WSOL_MINT, &crate::TOKEN_PROGRAM, &LaunchParams {
            decimals: 6,
            name: "AB",
            symbol: "C",
            uri: "",
            supply: 1,
            total_base_sell: 2,
            total_quote_fund_raising: 3,
        });
        // 8 disc + 1 dec + (4+2) + (4+1) + (4+0) + 1 curve + 24 + 1 migrate + 24 vesting + 1 + 1
        assert_eq!(ix.data.len(), 8 + 1 + 6 + 5 + 4 + 1 + 24 + 1 + 24 + 1 + 1);
        assert_eq!(ix.accounts.len(), 15);
        assert!(ix.accounts[0].is_signer && ix.accounts[6].is_signer && !ix.accounts[1].is_signer);
    }
}

// -- after graduation: Raydium CPMM and the LP lock program --------------------
// Layouts read from `raydium-cp-swap` at 59fb845, `raydium-sdk-V2` at c289783,
// and the devnet pool and lock accounts of a coin this program launched and
// Raydium migrated on 18 September 2026.

pub mod cpmm {
    use super::*;
    use crate::{CPMM_PROGRAM, LOCK_PROGRAM};
    use solana_program::program_error::ProgramError;

    pub const DISC_SWAP_BASE_INPUT: [u8; 8] = [143, 190, 90, 218, 196, 30, 51, 222];
    pub const DISC_COLLECT_CREATOR_FEE_PERMISSIONLESS: [u8; 8] = [202, 202, 34, 83, 226, 122, 145, 229];
    /// The SDK's `anchorDataBuf.collectCpFee`; it is the Anchor sighash of
    /// `collect_cp_fees` (the lock program's source is not public).
    pub const DISC_COLLECT_CP_FEE: [u8; 8] = [8, 30, 51, 199, 209, 184, 247, 133];
    pub const MEMO_PROGRAM: Pubkey = solana_program::pubkey!("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr");
    #[cfg(not(feature = "devnet"))]
    pub const LOCK_AUTH: Pubkey = solana_program::pubkey!("3f7GcQFG397GAaEnv51zR6tsTVihYRydnydDD1cXekxH");
    #[cfg(feature = "devnet")]
    pub const LOCK_AUTH: Pubkey = solana_program::pubkey!("7qWVV8UY2bRJfDLP4s37YzBPKUkVB46DStYJBpYbQzu3");

    pub fn authority() -> Pubkey {
        Pubkey::find_program_address(&[b"vault_and_lp_mint_auth_seed"], &CPMM_PROGRAM).0
    }
    /// Read by CPMM's permissionless creator-fee collect: a per-creator share
    /// of the creator fee (the config carries a default `creatorFeeShareRate`).
    /// Seeds from `raydium-sdk-v2` 0.2.70-alpha `getCreatorFeeSharePda`; not
    /// in the public `raydium-cp-swap` source as of 59fb845.
    pub fn creator_fee_share(creator: &Pubkey, amm_config: &Pubkey) -> Pubkey {
        Pubkey::find_program_address(&[b"creator_fee_share", creator.as_ref(), amm_config.as_ref()], &CPMM_PROGRAM).0
    }
    pub fn lock_position(nft_mint: &Pubkey) -> Pubkey {
        Pubkey::find_program_address(&[b"locked_liquidity", nft_mint.as_ref()], &LOCK_PROGRAM).0
    }

    /// The fields of a CPMM `PoolState` the cranks use.
    #[derive(Debug, Clone, Copy, PartialEq, Eq)]
    pub struct Pool {
        pub amm_config: Pubkey,
        pub pool_creator: Pubkey,
        pub vault0: Pubkey,
        pub vault1: Pubkey,
        pub lp_mint: Pubkey,
        pub mint0: Pubkey,
        pub mint1: Pubkey,
        pub prog0: Pubkey,
        pub prog1: Pubkey,
        pub observation: Pubkey,
    }
    pub fn read_pool(d: &[u8]) -> Result<Pool, ProgramError> {
        if d.len() < 328 {
            return Err(ProgramError::InvalidAccountData);
        }
        let k = |o: usize| Pubkey::new_from_array(d[o..o + 32].try_into().unwrap());
        Ok(Pool {
            amm_config: k(8),
            pool_creator: k(40),
            vault0: k(72),
            vault1: k(104),
            lp_mint: k(136),
            mint0: k(168),
            mint1: k(200),
            prog0: k(232),
            prog1: k(264),
            observation: k(296),
        })
    }

    /// The fields of an LP lock position the LP crank checks: pool at 64,
    /// Fee Key NFT mint at 96, LP mint at 160 (read from the devnet account).
    pub fn read_lock(d: &[u8]) -> Result<(Pubkey, Pubkey, Pubkey), ProgramError> {
        if d.len() < 192 {
            return Err(ProgramError::InvalidAccountData);
        }
        let k = |o: usize| Pubkey::new_from_array(d[o..o + 32].try_into().unwrap());
        Ok((k(64), k(96), k(160)))
    }

    pub fn collect_creator_fee_ix(payer: &Pubkey, creator: &Pubkey, pool_key: &Pubkey, p: &Pool, ata0: &Pubkey, ata1: &Pubkey) -> Instruction {
        Instruction {
            program_id: CPMM_PROGRAM,
            accounts: vec![
                AccountMeta::new(*payer, true),
                AccountMeta::new_readonly(*creator, false),
                AccountMeta::new_readonly(authority(), false),
                AccountMeta::new(*pool_key, false),
                AccountMeta::new(p.vault0, false),
                AccountMeta::new(p.vault1, false),
                AccountMeta::new_readonly(p.mint0, false),
                AccountMeta::new_readonly(p.mint1, false),
                AccountMeta::new(*ata0, false),
                AccountMeta::new(*ata1, false),
                AccountMeta::new_readonly(p.prog0, false),
                AccountMeta::new_readonly(p.prog1, false),
                AccountMeta::new_readonly(crate::tokenix::ATA_PROGRAM, false),
                AccountMeta::new_readonly(solana_program::system_program::id(), false),
                // Deployed CPMM (devnet, September 2026) takes these two
                // after the public source's 14; order from the SDK 0.2.70.
                AccountMeta::new_readonly(p.amm_config, false),
                AccountMeta::new_readonly(creator_fee_share(creator, &p.amm_config), false),
            ],
            data: DISC_COLLECT_CREATOR_FEE_PERMISSIONLESS.to_vec(),
        }
    }

    /// `swap_base_input`. `quote_is_0` says which side is the quote asset;
    /// the swap always goes quote -> coin (the own-token burn buy).
    #[allow(clippy::too_many_arguments)]
    pub fn buy_coin_ix(payer: &Pubkey, pool_key: &Pubkey, p: &Pool, quote_is_0: bool, user_quote: &Pubkey, user_coin: &Pubkey, amount_in: u64, min_out: u64) -> Instruction {
        let (in_vault, out_vault, in_prog, out_prog, in_mint, out_mint) = if quote_is_0 {
            (p.vault0, p.vault1, p.prog0, p.prog1, p.mint0, p.mint1)
        } else {
            (p.vault1, p.vault0, p.prog1, p.prog0, p.mint1, p.mint0)
        };
        let mut data = DISC_SWAP_BASE_INPUT.to_vec();
        data.extend_from_slice(&amount_in.to_le_bytes());
        data.extend_from_slice(&min_out.to_le_bytes());
        Instruction {
            program_id: CPMM_PROGRAM,
            accounts: vec![
                AccountMeta::new_readonly(*payer, true),
                AccountMeta::new_readonly(authority(), false),
                AccountMeta::new_readonly(p.amm_config, false),
                AccountMeta::new(*pool_key, false),
                AccountMeta::new(*user_quote, false),
                AccountMeta::new(*user_coin, false),
                AccountMeta::new(in_vault, false),
                AccountMeta::new(out_vault, false),
                AccountMeta::new_readonly(in_prog, false),
                AccountMeta::new_readonly(out_prog, false),
                AccountMeta::new_readonly(in_mint, false),
                AccountMeta::new_readonly(out_mint, false),
                AccountMeta::new(p.observation, false),
            ],
            data,
        }
    }

    /// The lock program's `CollectCpFee`, layout from the SDK's
    /// `collectCpFeeInstruction`. `owner` holds the Fee Key NFT and signs.
    #[allow(clippy::too_many_arguments)]
    pub fn collect_cp_fee_ix(owner: &Pubkey, nft_account: &Pubkey, nft_mint: &Pubkey, pool_key: &Pubkey, p: &Pool, owner_ata0: &Pubkey, owner_ata1: &Pubkey, lp_fee_amount: u64) -> Instruction {
        let lock_lp_vault = crate::tokenix::associated_token_address(&LOCK_AUTH, &p.lp_mint, &crate::TOKEN_PROGRAM);
        let mut data = DISC_COLLECT_CP_FEE.to_vec();
        data.extend_from_slice(&lp_fee_amount.to_le_bytes());
        Instruction {
            program_id: LOCK_PROGRAM,
            accounts: vec![
                AccountMeta::new_readonly(LOCK_AUTH, false),
                AccountMeta::new_readonly(*owner, true),
                AccountMeta::new(*nft_account, false),
                AccountMeta::new(lock_position(nft_mint), false),
                AccountMeta::new_readonly(CPMM_PROGRAM, false),
                AccountMeta::new_readonly(authority(), false),
                AccountMeta::new(*pool_key, false),
                AccountMeta::new(p.lp_mint, false),
                AccountMeta::new(*owner_ata0, false),
                AccountMeta::new(*owner_ata1, false),
                AccountMeta::new(p.vault0, false),
                AccountMeta::new(p.vault1, false),
                AccountMeta::new_readonly(p.mint0, false),
                AccountMeta::new_readonly(p.mint1, false),
                AccountMeta::new(lock_lp_vault, false),
                AccountMeta::new_readonly(crate::TOKEN_PROGRAM, false),
                AccountMeta::new_readonly(crate::TOKEN_2022_PROGRAM, false),
                AccountMeta::new_readonly(MEMO_PROGRAM, false),
            ],
            data,
        }
    }

    #[cfg(test)]
    mod tests {
        use super::*;
        #[test]
        fn cpmm_discriminators_are_anchor_sighashes() {
            use solana_program::hash::hash;
            for (name, disc) in [
                ("swap_base_input", DISC_SWAP_BASE_INPUT),
                ("collect_creator_fee_permissionless", DISC_COLLECT_CREATOR_FEE_PERMISSIONLESS),
                ("collect_cp_fees", DISC_COLLECT_CP_FEE),
            ] {
                assert_eq!(&hash(format!("global:{name}").as_bytes()).to_bytes()[..8], &disc, "{name}");
            }
        }
    }
}

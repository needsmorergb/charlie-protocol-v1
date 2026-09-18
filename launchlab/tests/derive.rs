// Every PDA is distinct, and the two System-owned signers are off-curve so
// only this program can sign for them.
use charlie_launchlab::*;
use solana_program::pubkey::Pubkey;

#[test]
fn pdas_are_distinct_and_off_curve() {
    let mint = Pubkey::new_from_array([1; 32]);
    let quote = Pubkey::new_from_array([2; 32]);
    let all = [
        platform_pda().0,
        signer_pda().0,
        collect_pda(&mint).0,
        route_pda(&mint).0,
        quote_pda(&quote).0,
        collect_pda(&quote).0,
    ];
    for (i, a) in all.iter().enumerate() {
        assert!(!a.is_on_curve());
        for b in &all[i + 1..] {
            assert_ne!(a, b);
        }
    }
}

#[test]
fn fee_parameters_are_the_designed_ones() {
    assert_eq!(PLATFORM_FEE_RATE * 10_000 / RAYDIUM_RATE_DENOMINATOR, 25, "0.25% in bps");
    assert_eq!(CREATOR_FEE_RATE * 10_000 / RAYDIUM_RATE_DENOMINATOR, 50, "0.50% in bps");
    assert_eq!(PLATFORM_SCALE + CREATOR_SCALE + BURN_SCALE, RAYDIUM_RATE_DENOMINATOR);
    assert_eq!(CREATOR_SCALE, 0, "LaunchLab refuses a creator LP slice");
    assert_eq!(LP_CHARLIE_BPS, 2_500);
    assert_eq!(ROUTE_TOTAL_BPS, 10_000);
}

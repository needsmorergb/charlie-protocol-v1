// Cross-checks the PDAs the Rust program derives against the ones the
// Python driver derives. The two implementations are independent, and a
// disagreement here is a bug that would otherwise show up as a wasted
// transaction on a funded wallet.
use charlie_protocol::*;
use solana_program::pubkey::Pubkey;
use std::str::FromStr;

#[test]
fn pdas_match_the_python_driver() {
    let mint = Pubkey::from_str("So11111111111111111111111111111111111111112").unwrap();
    assert_eq!(
        route_pda(&mint).0.to_string(),
        "6v5FVNJj17keH2nSaYyrV3JxV5W6gEG6qMsGFfsnU5gW"
    );
    assert_eq!(
        collector_pda(&mint).0.to_string(),
        "HHeLyMeUgfhW6HahHaCvuNJuUW4h8ZqtkXfGFxMETA3s"
    );
    assert_eq!(
        burn_pool_pda(&mint).0.to_string(),
        "HkwxMnM4vdqijb9dzNZFdAS4WaZiGokZu1x4by8CY1Kg"
    );
    assert_eq!(
        charlie_pool_pda().0.to_string(),
        "CJEC7BywJ4fgveTPRpPbF4NxSgAzZxs6Dgo9wk1mV5qL"
    );
}

#[test]
fn the_program_id_is_the_deployed_one() {
    assert_eq!(id().to_string(), "GFA3nG9geMhpPXaExVLGYBtj6aJX7S125dLzv4EcXGiG");
}

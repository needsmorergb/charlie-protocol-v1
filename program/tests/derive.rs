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
        "zydTepJui8eP9WvSDg45R8Cy4QVmwRboP9QgpTTjRZj"
    );
    assert_eq!(
        collector_pda(&mint).0.to_string(),
        "ErvsY3DwrPT1k85p6JDY9zqtefokQPNppUqh5BuAZcE9"
    );
    assert_eq!(
        burn_pool_pda(&mint).0.to_string(),
        "EyXWd5BHtLp9b2w1HDGwdfhNhdrVohqN8gkpfb8aj4mE"
    );
    assert_eq!(
        charlie_pool_pda().0.to_string(),
        "2XtBAvKaqwFD1Gbt4zu4nrHUzyh9afUdKYvEhYcSaY51"
    );
}

#[test]
fn the_program_id_is_the_deployed_one() {
    assert_eq!(id().to_string(), "6GfLJwxqBWHFeYjfJma3ZBtkRpcKLkZHVKcQ1s6CgSyJ");
}

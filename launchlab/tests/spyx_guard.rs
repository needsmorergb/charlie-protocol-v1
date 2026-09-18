// The guards against SPYx's real mint account, read from mainnet at slot
// 448,127,246 (18 September 2026). If Backed changes the mint's layout or
// its extensions, re-read it and this is where the difference shows.
use charlie_launchlab::guards::*;
use solana_program::pubkey::Pubkey;

// Stored as base64 text so every file in the repo can travel through a
// text-only channel (the GitHub connector's file push takes strings).
const SPYX_B64: &str = include_str!("fixtures/spyx_mint_slot448127246.b64");

fn spyx() -> Vec<u8> {
    const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = Vec::new();
    let (mut acc, mut bits) = (0u32, 0u32);
    for c in SPYX_B64.bytes().filter(|c| !c.is_ascii_whitespace() && *c != b'=') {
        let v = ALPHABET.iter().position(|a| *a == c).expect("base64 fixture") as u32;
        acc = (acc << 6) | v;
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            out.push((acc >> bits) as u8);
            acc &= (1 << bits) - 1;
        }
    }
    out
}

#[test]
fn live_spyx_passes_and_reads_right() {
    let st = check_quote_mint(&spyx()).expect("SPYx passes today");
    assert_eq!(st.decimals, 8);
    assert_eq!(st.hook_program, Some(Pubkey::default()), "hook extension present, program unset");
    assert!(!st.paused);
    let (m, at, nm) = st.scaled.unwrap();
    assert!((m - 1.003909240011759).abs() < 1e-15);
    assert_eq!(at, 1_781_755_200);
    assert!((nm - 1.005714560286254).abs() < 1e-15);
    assert_eq!(st.multiplier_at(at - 1), m);
    assert_eq!(st.multiplier_at(at), nm);
}

fn tlv_value_offset(data: &[u8], ty: u16) -> usize {
    let mut i = TLV_START;
    loop {
        let t = u16::from_le_bytes([data[i], data[i + 1]]);
        let l = u16::from_le_bytes([data[i + 2], data[i + 3]]) as usize;
        if t == ty {
            return i + 4;
        }
        i += 4 + l;
    }
}

#[test]
fn a_live_hook_is_refused() {
    let mut d = spyx();
    let v = tlv_value_offset(&d, EXT_TRANSFER_HOOK);
    d[v + 32] = 1; // Backed sets a hook program
    assert!(check_quote_mint(&d).is_err());
    assert!(read_quote_mint(&d).is_ok(), "readable, just not allowed");
}

#[test]
fn a_pause_is_refused() {
    let mut d = spyx();
    let v = tlv_value_offset(&d, EXT_PAUSABLE);
    d[v + 32] = 1;
    assert!(check_quote_mint(&d).is_err());
}

#[test]
fn legacy_mint_and_garbage() {
    let mut legacy = vec![0u8; BASE_MINT_LEN];
    legacy[44] = 6;
    let st = check_quote_mint(&legacy).unwrap();
    assert_eq!((st.decimals, st.hook_program, st.paused, st.scaled), (6, None, false, None));
    assert!(read_quote_mint(&spyx()[..60]).is_err());
    let mut bad = spyx();
    bad[ACCOUNT_TYPE_OFFSET] = 2; // a token account, not a mint
    assert!(read_quote_mint(&bad).is_err());
    let mut trunc = spyx();
    trunc.truncate(TLV_START + 10); // TLV claims more bytes than exist
    assert!(read_quote_mint(&trunc).is_err());
}

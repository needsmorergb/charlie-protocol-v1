#!/usr/bin/env bash
# One command to put the SOL-paired LaunchLab rail live on mainnet.
#
#   CHARLIE_WALLET=<buyback wallet> ./tools/launchlab_mainnet_go.sh
#
# Run from the repo root with charlie_launchlab_mainnet.so and
# charlie_launchlab-program-keypair.json in it. Uses your default Solana CLI
# wallet (override with KEYPAIR=...). Stops at the first failure, and asks
# before every step that spends SOL.
set -euo pipefail

PROGRAM=GM6ET1LceNLkHUWefD79eeeJzFRk8yQwfnR7pdYP5d6e
BUILD_SHA=f3182985a8fa3b4ee168bd501f650bdc5eec4c8ba4a0a326234e927a4c5851ef
CPMM_CONFIG=EUZHCdd8H7nueb2wLpxUqyuemdbe8TUxfCWcxVfARvgw   # 0.25% trade / 0.55% creator
KEYPAIR=${KEYPAIR:-$HOME/.config/solana/id.json}
URL=https://api.mainnet-beta.solana.com
: "${CHARLIE_WALLET:?set CHARLIE_WALLET to the buyback wallet}"

confirm() { read -r -p "$1 [y/N] " a; [[ "$a" == y || "$a" == Y ]] || { echo "stopped"; exit 1; }; }

echo "== checks"
WALLET=$(solana-keygen pubkey "$KEYPAIR")
echo "wallet $WALLET: $(solana balance "$WALLET" --url $URL)"
echo "charlie wallet $CHARLIE_WALLET: $(solana balance "$CHARLIE_WALLET" --url $URL) (needs >= 0.002 SOL)"
test "$(sha256sum charlie_launchlab_mainnet.so | cut -c1-64)" = "$BUILD_SHA" || { echo "binary hash mismatch"; exit 1; }
test "$(solana-keygen pubkey charlie_launchlab-program-keypair.json)" = "$PROGRAM" || { echo "program keypair mismatch"; exit 1; }

if ! solana program show $PROGRAM --url $URL >/dev/null 2>&1; then
  confirm "== deploy $PROGRAM (about 1.54 SOL kept as rent, 1.54 SOL buffer refunded)?"
  solana program deploy --url $URL --keypair "$KEYPAIR" \
    --program-id charlie_launchlab-program-keypair.json charlie_launchlab_mainnet.so \
    --with-compute-unit-price 50000 --max-sign-attempts 100 --use-rpc
fi
echo "== verify"
solana program show $PROGRAM --url $URL | grep -E "Authority|Data Length"
solana program dump $PROGRAM /tmp/charlie_deployed.so --url $URL >/dev/null
test "$(head -c 221384 /tmp/charlie_deployed.so | sha256sum | cut -c1-64)" = "$BUILD_SHA" \
  && echo "deployed bytes match the build" || { echo "DEPLOYED BYTES DIFFER"; exit 1; }

echo "== platform setup (simulation)"
ARGS=(--keypair "$KEYPAIR" --charlie-pool "$CHARLIE_WALLET" --cpmm-config $CPMM_CONFIG)
python3 -m tools.launchlab_mainnet_setup "${ARGS[@]}"
confirm "== send the setup transactions? (charlie wallet is FIXED after this)"
for i in 1 2 3 4; do python3 -m tools.launchlab_mainnet_setup "${ARGS[@]}" --send | tee /tmp/charlie_setup.log; \
  grep -q "lookup table [1-9A-HJ-NP-Za-km-z]\{32,\}" /tmp/charlie_setup.log && break; done
echo
echo "Live. Keeper: python3 -m indexer.launchlab_keeper --cluster mainnet --keypair $KEYPAIR --lookup-table <table above> [--send]"

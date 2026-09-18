# LaunchLab rail: mainnet runbook

Program id **`ENZrfqk89oSQ2fPHPmi6NSMuRDo7F3txEZhi8ZVbCHym`**. Build
`charlie_launchlab_mainnet.so`, SHA-256
`07a1e59dc03576da8013afb583ca4b27bdf94e68038cdbcdc2be19571e755855`, 221,384 bytes.

Your wallet signs everything. It becomes the program's upgrade authority and the
platform's admin (the only key that can run the cranks).

## Checked on mainnet before this was written (18 September 2026, read-only)

- LaunchLab platform creation is permissionless (2,437 platforms exist); the
  rail's exact platform parameters simulate OK against two CPMM configs.
- The SOL global config the rail launches through exists:
  `6s1xP3hpbAfFoNtUNF8mfHsjr2Bd97JxFJRWLbL6aHuX`.
- The rail's curve (1B supply, 793.1M sold) simulates OK with 85 and 30 SOL raises.
- The build contains mainnet Raydium addresses and no devnet ones; all tests pass
  for both builds.

## What you need

- Solana CLI (`solana --version`), your wallet as a keypair file, Python 3.10+,
  and this repository with patches 0001-0013 applied.
- About **3.3 SOL**: 1.542 program rent (kept), 1.542 upload buffer (refunded
  when the deploy finishes), ~0.03 for setup accounts and fees, plus 0.01 per coin
  launched. Keep extra for deploy retries.

## Decide first

1. **Where the Charlie leg's SOL goes** (`--charlie-pool`). Every coin's 0.25%
   platform fee and 25% of LP fees are paid here. Fixed at step 3; changing it
   later needs a program upgrade.
2. **Pool fee after graduation** (`--cpmm-config`). Default is Raydium's
   index 0: 0.25% trade fee, 0.05% creator fee (the creator fee funds each
   coin's burn/ops legs). Index 8 (`EUZHCdd8H7nueb2wLpxUqyuemdbe8TUxfCWcxVfARvgw`)
   is 0.25% trade and 0.55% creator. Fixed once the platform is created.
3. **`--min-crank`** in lamports (default 0.01 SOL): the smallest fee a crank
   will route.

## Steps

```bash
# 1. Deploy (your wallet becomes upgrade authority)
solana program deploy --url mainnet-beta --keypair ~/.config/solana/id.json \
  --program-id charlie_launchlab-program-keypair.json charlie_launchlab_mainnet.so \
  --with-compute-unit-price 50000 --max-sign-attempts 100 --use-rpc
solana program show ENZrfqk89oSQ2fPHPmi6NSMuRDo7F3txEZhi8ZVbCHym --url mainnet-beta
#    check "Authority" is your wallet

# 2. Verify the deployed bytes are this build
solana program dump ENZrfqk89oSQ2fPHPmi6NSMuRDo7F3txEZhi8ZVbCHym deployed.so --url mainnet-beta
head -c 221384 deployed.so | sha256sum      # must print f3182985...51ef

# 3. Platform setup: simulate, then send
python -m tools.launchlab_mainnet_setup --keypair ~/.config/solana/id.json --charlie-pool <ADDRESS>
python -m tools.launchlab_mainnet_setup --keypair ~/.config/solana/id.json --charlie-pool <ADDRESS> --send
#    rerun the --send line until all four steps say done; it prints the lookup table address

# 4. Launch a coin: simulate, then send
python -m tools.launchlab_launch --keypair dev.json --name NAME --symbol SYM \
  --uri https://.../metadata.json --ops-address <FUNDED WALLET>
#    add --send; the mint's keypair is saved next to dev.json

# 5. Keeper: simulate, then send; run on a schedule (e.g. every 10 minutes)
python -m indexer.launchlab_keeper --cluster mainnet --keypair ~/.config/solana/id.json \
  --lookup-table <TABLE>
#    add --send
```

## Known limits you are accepting

- **One key controls everything.** Your wallet can upgrade the program and is
  the only crank signer. If it leaks, the program can be replaced. Keep the file
  offline except when running these commands.
- **The own-burn buy has no on-chain price bound.** A block producer could
  sandwich it. Cranking often keeps each buy small.
- **The ops address must already hold SOL.** A payment that would leave a
  never-funded ops wallet below rent-exempt reverts the whole crank.
- **Unaudited.** Devnet runs covered every crank; no external review has.
- **Raydium and pump hold their own admin levers** (pool creator-fee share,
  pump `admin_cto`); both are recorded in LAUNCHLAB-RAIL.md.

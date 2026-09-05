# Handoff

State of the work on 2026-09-05, written for whoever picks it up next, human
or agent. Everything below is either live on main in both repositories or
named as not done. Spelling is American throughout: enroll, enrollment.

## What is live

- **Enrollment works front to back** at https://charlieprotocol.fun/enroll.
  A coin's creator connects a wallet, the page reads the chain, and one
  signature creates the pump fee-sharing config and sets the split. The
  server (`api/enroll.py` → `indexer/enroll.py`) refuses any split that does
  not pay the protocol's collection wallet at least 5%.
- **The 5% is set.** `indexer/legs.py`: `TOLL_DESTINATION =
  8SvEu1bvkhgaSkZW4XHLzfw8djd748KAVHMwvkYGfyr8`, `TOLL_BPS = 500`. pump
  enforces the split on chain; after the one-shot update it is permanent.
- **Enrolled has one meaning**: the coin's on-chain split pays
  `TOLL_DESTINATION` ≥ `TOLL_BPS`. `indexer/invariants.py::protocol_share`
  is the `PROTOCOL_SHARE` check on every coin record (10 checks now). Every
  coin page carries an enrollment section (`site._enrollment`), the index an
  enrolled / not-enrolled marker, the landing page the explanation.
- **The crank** `indexer/distribute.py` pays every enrolled coin's
  shareholders from its vault with pump's permissionless
  `distribute_creator_fees`, preceded for a graduated coin by the AMM
  transfer that moves its wSOL into that vault. `python -m indexer distribute --all-enrolled`
  runs hourly from the site repo's `distribute.yml`. Without a key it
  builds and simulates only, standing pump's own fee wallet in as the payer
  (`STAND_IN_PAYER`), because a simulation needs a payer that exists and the
  collection wallet holds no SOL.
- **Proven against mainnet** by the site repo's `enroll.yml`: the exact
  transaction the page builds simulates OK with the real toll wallet and
  leaves exactly the requested split; the crank's payout simulates OK on
  real coins with funded vaults; the `deployed` job asks production the
  questions a browser asks.

## Open items, in order

1. **Add the repository secret `CHARLIE_CRANK_KEYPAIR`** in
   charlie-protocol-site: the JSON key array of a separate low-value wallet
   holding about 0.05 SOL for network fees, nothing else. Until it exists
   the hourly crank simulates and sends nothing. Never generate this key in
   a session; the owner makes it.
2. **Graduated coins are paid, with one case left.** The crank prepends
   `pump_amm::transfer_creator_fees_to_pump` for a graduated coin, and a
   coin that graduated before enrolling can enroll (the create passes the
   canonical pool, which also migrates the pool's coin creator). Both are
   simulated against mainnet in the site's `enroll` workflow. Still refused:
   a pool whose `coin_creator` is not the sharing config (the census found
   one, a zero pubkey on a pool older than the field). Paying it needs
   `pump_amm::migrate_pool_coin_creator`, permissionless, not built.
3. **No on-chain program enforces the 5% at enrollment time.** The page
   and the server refuse without it, and pump makes the split permanent
   once set, but a creator who builds their own transaction can set a
   split without us. That coin simply reads "not enrolled". The program
   is the long-term answer; `PROTOCOL.md` and `ARCHITECTURE.md` describe it.
4. **RPC rate limiting.** The gateway (`https://crowd-api-gateway.vercel.app/`)
   answers 429 after a few chain-reading workflow runs in quick succession.
   Space out manual dispatches of `enroll`, `intake`, `distribute`. A 429
   in the crank step surfaces as `RpcUnavailable` and fails the step
   loudly; that is by design so far, not a bug to hide.
5. **Dispatching `enroll.yml`'s `deployed` job right after a merge races
   Vercel.** Wait until the production deployment is READY (about a minute)
   or the grep for the new page fails against the old one.

## How the two repositories relate

- `charlie-protocol-v1` is the source. `charlie-protocol-site` is the
  deploy: Vercel serves `web/`, the functions in `api/`, and the workflows
  run the indexer against mainnet.
- `tools/shared_sync.py` owns the list of shared files (36 today:
  `indexer/*`, `api/*`, `vercel.json`, `web/assets`). CI in both repos
  fails on drift. After changing a shared file here:
  `python tools/shared_sync.py --copy-to ../charlie-protocol-site`, then
  commit there too.
- The site commits the text evidence export (`state/evidence/`), never
  the binary db. `intake.yml` loads it, measures the queue, regenerates
  pages, exports back.
- Generated pages under `web/` in the site repo must equal what the
  renderer produces (`tests/test_committed_pages.py`, run with
  `CHARLIE_SITE_REPO`). Merge conflicts on them are resolved by taking
  the newer renderer's output; the next intake regenerates everything.

## Running the checks

```
# in charlie-protocol-v1, with the site checked out beside it
CHARLIE_REQUIRE_NODE=1 CHARLIE_SITE_REPO=../charlie-protocol-site python -m unittest discover -s tests -q
python tools/shared_sync.py --against ../charlie-protocol-site
# in charlie-protocol-site
python -m unittest discover -s tests -q
```

740 tests pass as of this handoff. The sandbox these sessions run in has no
Solana RPC; every chain read happens in GitHub Actions. Production can be
fetched at the custom domain; branch previews sit behind Vercel's login.

## Conventions worth knowing before editing

- Pushes to `main` are refused from a session; open a PR and merge it.
- `tests/test_discipline.py` is an allowlist of SQL sites; a new query
  must be added there deliberately.
- `tests/test_intake.py` forbids the identifier "enrolled" in
  `indexer/observe.py`; the check is named `protocol_share` for that reason.
- Every emitter of a figure is classified in `indexer/publish.py::SURFACES`;
  a new `print` of a number needs an entry.
- Nothing is marked green because it is probably fine. A check that cannot
  run is `UNCHECKED` and withholds its figure.

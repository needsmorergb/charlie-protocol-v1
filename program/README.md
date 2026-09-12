# program/ — written, on devnet, not deployed to mainnet

The on-chain half of [`FEE-ROUTING.md`](../FEE-ROUTING.md). Four instructions:

```
init_charlie_pool()                                    -> creates charlie_pool = PDA(["charlie"]), stores TOLL_BPS
init_route(sol_burn_bps, own_burn_bps, ops_bps, ops)   -> creates route(mint) and collector(mint)
set_route(sol_burn_bps, own_burn_bps, ops_bps, ops)    -> the dev's split, changeable any day
distribute()                                           -> splits the collector four ways
```

The guarantee comes from the *absence* of code. A stranger can read
[`src/lib.rs`](src/lib.rs) and confirm three things:

* nothing moves lamports out of `collector(mint)` except `distribute`;
* nothing moves lamports out of `charlie_pool` or `burn_pool(mint)` except the cranks;
* no instruction anywhere sends to an address its caller supplied.

Every destination `distribute` pays is re-derived from the mint or read from the
coin's own `route` account, so a caller cannot redirect a lamport of it. The SOL
burn leg pays Solana's incinerator, which is a hardcoded constant, not an
argument. `ops_address` comes out of `route`, never off the wire.

`TOLL_BPS` is 2500 — 25%. It is a constant in this code and is not a field of
any account a dev can write. It is ALSO stored in `charlie_pool` at
initialisation and asserted against the constant on every `distribute`, because
a constant compiled into BPF is not on-chain state, and a protocol that asks
strangers to recompute its figures cannot first ask them to reproduce a build
(`BUILD.md` §3). If the two ever diverge the program refuses rather than paying
a rate nobody published.

No borsh, no anchor. Hand-rolled little-endian encoding, because the point of
this program is that a stranger can read it, and a derive macro they have to
trust is a worse guarantee than 20 lines they can check.

## Status

**Written.** 11 tests pass (`cargo test`), covering the properties the design
claims: the toll is unmoved by any dev split, legs never exceed what came in,
and the integer-division remainder stays in the collector rather than favouring
whichever leg the code happens to pay first.

**Deployed to devnet**, at the id in `declare_id!`:

```
GFA3nG9geMhpPXaExVLGYBtj6aJX7S125dLzv4EcXGiG   (devnet)
```

That is a devnet id and nothing else. **Mainnet gets its own keypair**,
generated once and deployed to after the pipeline has been run in production
(`BUILD.md` §10 — the deploy order is deliberate).

**Not deployed to mainnet.** That is phase 5, and it is funding-gated and
closed. The absence-of-code guarantee only holds once the program is
**immutable**: upgrade authority must be revoked before any coin enrolls, and
revoking it is a one-way door that freezes every bug permanently.

Until mainnet is deployed there is no mainnet program id, so no mainnet address
derives as a SOL-burn or token-burn vault. `indexer/legs.py` holds
`PROGRAM_ID = None` for exactly that reason, and `derive` says so rather than
guessing.

# program/ — not built

Two instructions, described in `ARCHITECTURE.md` §2 and planned in
[`../ROADMAP.md`](../ROADMAP.md) §3.

```
init_vault(mint)   -> creates seal_vault = PDA(["seal", mint]) and burn_pool = PDA(["burn", mint])
crank_burn(mint)   -> atomic: pull -> swap -> SPL burn
```

The guarantee comes from the *absence* of code: a PDA can only be signed for by
its owning program, and this program will contain no instruction that moves
lamports out of a `seal` PDA. Anyone can verify that by reading it.

That only holds if the program is **immutable**. Upgrade authority must be
revoked before any coin enrolls, and revoking it is a one-way door that freezes
every bug permanently. The gate for walking through it is `ROADMAP.md` §2.5.

Until it is deployed there is no program id, so no address anywhere derives as a
seal or burn vault. `indexer/legs.py` holds `PROGRAM_ID = None` for exactly that
reason, and `derive` says so rather than guessing.

"""Pre-ground mint keypairs for the launch door.

A coin launched through Charlie gets a mint address ending in the protocol's
suffix. Grinding such a key takes billions of tries (`tools/vanity_mint.py`,
on a GPU), so they are made ahead of time and the door hands them out.

The door runs as a serverless function with no disk that survives a request,
so the pool is configuration, not a file: `CHARLIE_MINT_POOL` holds the seeds,
comma-separated, each base58 of 32 bytes (a seed) or 64 (seed and public
key, Solana's keypair layout). Which of them are spent is not tracked here
either -- the chain already knows. A key is spent once its mint account
exists, so `pick` asks the node about every address in one call and returns
the first that is still empty. A key that was handed out but never signed
and sent is therefore reused, which is what we want.

Two builds in the same second can be handed the same key; the second `create`
then fails as "already taken" and the page asks again, the same path a random
collision would take. With one launch a day that race is theoretical.

The suffix is a brand mark, not proof of origin: anyone can grind one. The
enrollment on chain is the proof. Nothing here changes that.
"""

from __future__ import annotations

import os

from . import ed25519
from .base58 import decode

ENV = "CHARLIE_MINT_POOL"


class PoolError(ValueError):
    """The pool is misconfigured or has no key left."""


def parse(text: str) -> list[ed25519.Keypair]:
    """Keypairs from the comma-separated env value. Empty text is an empty
    pool; a malformed entry is an error, never a silently shorter pool."""
    pool: list[ed25519.Keypair] = []
    for index, item in enumerate(part.strip() for part in text.split(",")):
        if not item:
            continue
        try:
            pool.append(ed25519.Keypair.from_secret_bytes(decode(item)))
        except Exception as exc:  # noqa: BLE001 -- say which entry, never its value
            raise PoolError(f"{ENV} entry {index + 1} is not a keypair: {exc}") from exc
    seen = set()
    for pair in pool:
        if pair.address in seen:
            raise PoolError(f"{ENV} lists {pair.address} twice")
        seen.add(pair.address)
    return pool


def configured() -> list[ed25519.Keypair] | None:
    """The pool from the environment, or None when the variable is unset --
    which means "no pool", and the door falls back to a random mint. Set but
    empty is a pool with nothing left, which refuses."""
    text = os.environ.get(ENV)
    if text is None:
        return None
    return parse(text)


def pick(pool: list[ed25519.Keypair], rpc) -> ed25519.Keypair:
    """The first key whose mint account does not exist yet. One
    `getMultipleAccounts` for the whole pool."""
    if not pool:
        raise PoolError("the mint pool is empty: grind more keys with tools/vanity_mint.py")
    accounts = rpc.accounts([pair.address for pair in pool])
    for pair, account in zip(pool, accounts):
        if account is None:
            return pair
    raise PoolError(f"every one of the {len(pool)} pre-ground mints has been used: grind more with tools/vanity_mint.py")


__all__ = ["ENV", "PoolError", "configured", "parse", "pick"]

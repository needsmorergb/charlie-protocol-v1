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

import json
import os
from pathlib import Path

from . import ed25519
from .base58 import decode

ENV = "CHARLIE_MINT_POOL"

# The mark. Every key in the pool must carry it: a pool entry that does not is
# a paste error, and the door would otherwise launch a coin without the mark
# while believing it had one.
SUFFIX = "1nc1n"


class PoolError(ValueError):
    """The pool is misconfigured or has no key left."""


def parse(text: str, suffix: str | None = None) -> list[ed25519.Keypair]:
    """Keypairs from the comma-separated env value. Empty text is an empty
    pool; a malformed entry, a duplicate, or an address without the suffix
    is an error, never a silently shorter pool."""
    if suffix is None:
        suffix = SUFFIX
    pool: list[ed25519.Keypair] = []
    for index, item in enumerate(part.strip() for part in text.split(",")):
        if not item:
            continue
        try:
            pool.append(ed25519.Keypair.from_secret_bytes(decode(item)))
        except Exception as exc:  # noqa: BLE001 -- say which entry, never its value
            raise PoolError(f"{ENV} entry {index + 1} is not a keypair: {exc}") from exc
        if not pool[-1].address.endswith(suffix):
            raise PoolError(f"{ENV} entry {index + 1} is {pool[-1].address}, which does not end in {suffix}")
    seen = set()
    for pair in pool:
        if pair.address in seen:
            raise PoolError(f"{ENV} lists {pair.address} twice")
        seen.add(pair.address)
    return pool


def from_file(path, suffix: str | None = None) -> list[ed25519.Keypair]:
    """Keypairs from the grinder's file (`tools/vanity_mint.py`): a JSON list
    of {"address", "keypair"}. Each keypair must derive to its address and
    carry the suffix; errors name the entry, never its value."""
    if suffix is None:
        suffix = SUFFIX
    entries = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(entries, list):
        raise PoolError(f"{path} is not a list of keypairs")
    pool: list[ed25519.Keypair] = []
    for index, entry in enumerate(entries):
        try:
            pair = ed25519.Keypair.from_secret_bytes(bytes(entry["keypair"]))
        except Exception as exc:  # noqa: BLE001 -- say which entry, never its value
            raise PoolError(f"{path} entry {index + 1} is not a keypair: {type(exc).__name__}") from None
        if pair.address != entry.get("address") or not pair.address.endswith(suffix):
            raise PoolError(f"{path} entry {index + 1} is {pair.address}, not its listed address with {suffix}")
        pool.append(pair)
    if len({pair.address for pair in pool}) != len(pool):
        raise PoolError(f"{path} lists an address twice")
    return pool


def configured() -> list[ed25519.Keypair] | None:
    """The pool from the environment, or None when the variable is unset --
    which means "no pool", and the door falls back to a random mint. Set but
    empty is a pool with nothing left, which refuses."""
    text = os.environ.get(ENV)
    if text is None:
        return None
    return parse(text)


def unused(pool: list[ed25519.Keypair], rpc) -> list[ed25519.Keypair]:
    """The keys whose mint account does not exist yet, in pool order. One
    `getMultipleAccounts` for the whole pool."""
    if not pool:
        return []
    accounts = rpc.accounts([pair.address for pair in pool])
    return [pair for pair, account in zip(pool, accounts) if account is None]


def pick(pool: list[ed25519.Keypair], rpc) -> ed25519.Keypair:
    """The first unused key, or a PoolError that says how to fix it."""
    if not pool:
        raise PoolError("the mint pool is empty: grind more keys with tools/vanity_mint.py")
    left = unused(pool, rpc)
    if not left:
        raise PoolError(f"every one of the {len(pool)} pre-ground mints has been used: grind more with tools/vanity_mint.py")
    return left[0]


def describe(rpc) -> dict:
    """What the page shows before a wallet connects: whether a pool is
    configured, how many keys are left, and the mark they carry. `left` is
    None when the chain could not be asked; the page treats that as unknown,
    not as zero, and the build itself asks again."""
    pool = configured()
    if pool is None:
        return {"pool": False, "suffix": SUFFIX, "size": 0, "left": None}
    try:
        left = len(unused(pool, rpc))
    except Exception:  # noqa: BLE001 -- availability is advisory here
        left = None
    return {"pool": True, "suffix": SUFFIX, "size": len(pool), "left": left}


__all__ = ["ENV", "SUFFIX", "PoolError", "configured", "describe", "parse", "pick", "unused"]

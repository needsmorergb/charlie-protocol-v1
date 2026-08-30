"""Burn-side decoding: SPL `burn`/`burnChecked`, and pump's Anchor events.

RESEARCH.md Q3/Q4/Q5, mainnet-verified this phase. Two decode paths, both
untrusted-input surfaces (`indexer/pump.py`'s module docstring: an address or a
log line existing is not evidence of anything):

* `find_burns` reads the RPC's own `jsonParsed` instruction output -- it does
  NOT hand-roll SPL Token instruction bytes (RESEARCH.md's "State of the Art"
  section explains why: the RPC already parses `amount`/`mint`/`authority` for
  us, and hand-rolling it would just be a second, unnecessary place to get the
  discriminator wrong). The one thing it must not do is trust the RPC's
  human-readable `"program"` label -- Pitfall 1: that label reads `"spl-token"`
  for BOTH the classic Token program and Token-2022, and every one of
  $CHARLIE's real burns runs on Token-2022. Filtering on `programId` instead
  is what makes this decoder see them.
* `decode_boost_event`/`decode_create_event` read raw `Program data:` log
  payloads and must check the full 8-byte Anchor discriminator before trusting
  a single field, exactly as `pump._raw()` checks owner + discriminator before
  trusting an account's bytes. `BuyEvent` is emitted immediately before
  `BoostBuyAndBurnEvent` in every real crank; only an exact discriminator
  match tells them apart.
"""

from __future__ import annotations

import base64
import hashlib

from .base58 import encode
from .pump import DecodeError, PUMP_AMM_PROGRAM, TOKEN_2022_PROGRAM, TOKEN_PROGRAM

# Anchor's event discriminator formula: sha256("event:" + EventName)[:8].
# Pinned as literals (not computed at import time) so a typo in either the
# constant or the name fails a test rather than silently matching nothing --
# `anchor_discriminator()` is tested against each of these independently.
DISC_BOOST_BUY_AND_BURN = bytes.fromhex("3f451c16305cc2b9")
DISC_CREATE_EVENT = bytes.fromhex("1b72a94ddeeb6376")
DISC_BUY_EVENT = bytes.fromhex("67f4521f2cf57777")  # recognised so it is skipped, not decoded


def anchor_discriminator(name: str) -> bytes:
    """`sha256(b"event:" + name)[:8]` -- Anchor's event-discriminator formula."""
    return hashlib.sha256(b"event:" + name.encode("utf-8")).digest()[:8]


def _need(data: bytes, cursor: int, length: int, what: str) -> None:
    if cursor + length > len(data):
        raise DecodeError(
            f"payload is {len(data)} bytes, too short to hold {what} at offset {cursor}"
        )


# -- burn instructions (jsonParsed) ----------------------------------------
def _flatten(tx: dict):
    """Yield `(flat_index, top_level_index, instruction)` for every
    instruction in the tree, in a fixed, repeatable order: top-level
    instructions first (in order), then each `meta.innerInstructions` group
    in order, each instruction within a group in order.

    `flat_index` is stable within one transaction -- the same tx, fetched
    again, walks in the same order -- which is what lets it serve as part of
    `burn_event`'s primary key (RESEARCH.md Q7).
    """
    flat_index = 0
    message = (tx.get("transaction") or {}).get("message") or {}
    top_level = message.get("instructions") or []
    for top_index, instr in enumerate(top_level):
        yield flat_index, top_index, instr
        flat_index += 1
    for entry in (tx.get("meta") or {}).get("innerInstructions") or []:
        top_index = entry.get("index")
        for instr in entry.get("instructions") or []:
            yield flat_index, top_index, instr
            flat_index += 1


def find_burns(tx: dict, mint: str) -> list[dict]:
    """Every `burn`/`burnChecked` instruction against `mint`, anywhere in the
    instruction tree -- top-level or nested inside a CPI (the boost burn sits
    one hop down inside `boost_buy_and_burn`).

    Filters on `programId` against the classic Token program and Token-2022,
    never on the RPC's `"program"` friendly label (Pitfall 1: that label is
    identical for both, and $CHARLIE's burns are Token-2022).
    """
    found = []
    for flat_index, top_index, instr in _flatten(tx):
        if not isinstance(instr, dict):
            continue
        if instr.get("programId") not in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
            continue
        parsed = instr.get("parsed")
        if not isinstance(parsed, dict) or parsed.get("type") not in ("burn", "burnChecked"):
            continue
        info = parsed.get("info") or {}
        if info.get("mint") != mint:
            continue
        found.append(
            {
                "instruction_index": flat_index,
                "top_index": top_index,
                "amount": int(info["amount"]),  # Pitfall 2: always a JSON string
                "account": info.get("account"),
                "authority": info.get("authority"),
                "type": parsed.get("type"),
            }
        )
    return found


def find_swap_shaped(tx: dict) -> bool:
    """True iff the transaction contains a swap-shaped instruction anywhere in
    its tree -- top-level or nested inside a CPI.

    RESEARCH.md Q6's algorithm, step 4: "a swap-shaped instruction -- a token
    transfer/transferChecked moving the quote side, or a recognised AMM
    program invocation." Two independent signals, either sufficient:

    * an invocation of `pump.PUMP_AMM_PROGRAM` (the recognised AMM program;
      extendable to a future DEX without changing the calling convention);
    * a `transfer`/`transferChecked` instruction against the classic Token
      program or Token-2022 -- the swap leg of a real boost crank is exactly
      this, one CPI hop inside `boost_buy_and_burn`.

    This is `BURN_ATOMIC`'s (EVID-09) one positive signal: finding this
    alongside a burn for the mint anywhere in the same transaction is the
    proof of atomicity PROTOCOL.md sec.4 requires (Solana transactions are
    all-or-nothing, so both instructions committing together is the whole
    proof).
    """
    for _flat_index, _top_index, instr in _flatten(tx):
        if not isinstance(instr, dict):
            continue
        if instr.get("programId") == PUMP_AMM_PROGRAM:
            return True
        parsed = instr.get("parsed")
        if isinstance(parsed, dict) and parsed.get("type") in ("transfer", "transferChecked"):
            if instr.get("programId") in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
                return True
    return False


# -- Program data: log lines -------------------------------------------------
def program_data_lines(tx: dict):
    """The base64 payload of every `Program data:` line in `meta.logMessages`."""
    prefix = "Program data: "
    for line in (tx.get("meta") or {}).get("logMessages") or []:
        if isinstance(line, str) and line.startswith(prefix):
            yield line[len(prefix) :]


# -- BoostBuyAndBurnEvent -----------------------------------------------------
# Layout after the 8-byte discriminator (RESEARCH.md Q4, from pump's official
# AMM IDL and confirmed against a live mainnet decode):
#   timestamp i64 @ 8:16
#   mint pubkey @ 16:48, bonding_curve @ 48:80, pool @ 80:112, authority @ 112:144
#   quote_amount_in_requested u64 @ 144:152
#   quote_amount_in_used u64 @ 152:160   -- the SOL spent
#   base_amount_burned u64 @ 160:168     -- EVID-06's "tokens burned" figure
_BOOST_EVENT_LEN = 168


def decode_boost_event(payload_b64: str) -> dict | None:
    """`None` unless the payload's first 8 bytes are exactly
    `DISC_BOOST_BUY_AND_BURN` -- in particular, a `BuyEvent` payload (which
    precedes the boost event in every real crank) decodes to `None`, never to
    garbage. Raises `DecodeError` on a payload too short for the struct it
    claims to be, rather than reading past the buffer.
    """
    raw = base64.b64decode(payload_b64)
    if raw[:8] != DISC_BOOST_BUY_AND_BURN:
        return None
    _need(raw, 0, _BOOST_EVENT_LEN, "BoostBuyAndBurnEvent")
    timestamp = int.from_bytes(raw[8:16], "little", signed=True)
    mint = encode(raw[16:48])
    bonding_curve = encode(raw[48:80])
    pool = encode(raw[80:112])
    authority = encode(raw[112:144])
    quote_amount_in_requested = int.from_bytes(raw[144:152], "little")
    quote_amount_in_used = int.from_bytes(raw[152:160], "little")
    base_amount_burned = int.from_bytes(raw[160:168], "little")
    return {
        "timestamp": timestamp,
        "mint": mint,
        "bonding_curve": bonding_curve,
        "pool": pool,
        "authority": authority,
        "quote_amount_in_requested": quote_amount_in_requested,
        "sol_spent": quote_amount_in_used,
        "tokens_burned": base_amount_burned,
    }


# -- CreateEvent --------------------------------------------------------------
def _read_borsh_string(data: bytes, cursor: int) -> tuple[str, int]:
    """A Borsh string: u32 LE length, then that many UTF-8 bytes.

    The only attacker-influenced lengths this module introduces (T-01-09) --
    bounded against the remaining buffer before slicing, mirroring
    `pump.read_sharing_config()`'s `MAX_SHAREHOLDERS` guard.
    """
    _need(data, cursor, 4, "a Borsh string length prefix")
    length = int.from_bytes(data[cursor : cursor + 4], "little")
    cursor += 4
    _need(data, cursor, length, "a Borsh string body")
    return data[cursor : cursor + length].decode("utf-8"), cursor + length


def decode_create_event(payload_b64: str) -> dict | None:
    """`None` unless the payload's first 8 bytes are exactly
    `DISC_CREATE_EVENT`. Reads `token_total_supply` -- EVID-07's figure --
    after three variable-length Borsh strings (`name`, `symbol`, `uri`) and a
    handful of fixed-size fields this phase does not need individually.
    """
    raw = base64.b64decode(payload_b64)
    if raw[:8] != DISC_CREATE_EVENT:
        return None
    cursor = 8
    name, cursor = _read_borsh_string(raw, cursor)
    symbol, cursor = _read_borsh_string(raw, cursor)
    _uri, cursor = _read_borsh_string(raw, cursor)
    _need(raw, cursor, 32, "CreateEvent.mint")
    mint = encode(raw[cursor : cursor + 32])
    cursor += 32
    cursor += 32 * 3  # bonding_curve, user, creator
    cursor += 8  # timestamp
    cursor += 8 * 3  # virtual_token_reserves, virtual_sol_reserves, real_token_reserves
    _need(raw, cursor, 8, "CreateEvent.token_total_supply")
    token_total_supply = int.from_bytes(raw[cursor : cursor + 8], "little")
    return {"mint": mint, "name": name, "symbol": symbol, "token_total_supply": token_total_supply}

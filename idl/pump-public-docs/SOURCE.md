# pump's published IDL, extracted

`pump.extract.json` is the `BondingCurve` type, the `AdminCtoEvent` type and
that event's discriminator, copied field for field from `pump-fun/pump-public-docs`
`idl/pump.json` at commit `e0687ae` (12 September 2026, "refresh pump, pump_amm
and pump_fees IDLs"). The docs repository's `main` was at `8109141`
(14 September) when this was taken, with no later change to that file.

`source_sha256` is the hash of the whole upstream file, so anyone can fetch it
and confirm the extract came from it. The full file is 268 KB and this code
reads three definitions out of it.

This is pump's PUBLISHED IDL, not the on-chain Anchor IDL account the root
`_idl_*.json` files were read from. It is here so `tests/test_quote_fields.py`
can recompute every offset `indexer/pump.py` and `indexer/decode.py` use for
the fields pump added in September, rather than trusting a number typed into
a comment. When the on-chain IDL account is re-read and agrees, replace it.

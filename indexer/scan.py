"""Backward-paginating multi-destination inflow scan (D-02).

Re-implements the pagination and balance-delta logic of
`charlie_xbot/src/charlie/scan.py` against the indexer's multi-destination
model -- see `01-CONTEXT.md`'s "Claude's Discretion" note: extract the
behaviour and the reasoning, do not import the private repo.

The one property everything else depends on: **the cursor never advances past
a signature this scan failed to fetch.** A rate-limited or otherwise broken
tick under-scans *visibly* -- the next run resumes at the same point and the
affected checks read `UNCHECKED` -- rather than *silently*, which would be a
gap in the record indistinguishable from "nothing happened here".
"""

from __future__ import annotations

DEFAULT_PAGE_LIMIT = 1000
BACKFILL_PAGES_PER_RUN = 5


def account_keys(tx: dict) -> list[str]:
    """The full, balance-array-order account list for a jsonParsed transaction.

    RESEARCH.md Q2: with `encoding: jsonParsed`, `accountKeys` already merges
    every address-lookup-table entry, so extending with
    `meta.loadedAddresses` is a safe no-op today. It is kept anyway -- a
    provider that returns the static keys only, with the loaded addresses
    separate, is handled identically without a code change.
    """
    message = (tx.get("transaction") or {}).get("message") or {}
    keys: list[str] = []
    for entry in message.get("accountKeys") or []:
        if isinstance(entry, dict):
            keys.append(entry.get("pubkey"))
        else:
            keys.append(entry)
    loaded = (tx.get("meta") or {}).get("loadedAddresses") or {}
    keys.extend(loaded.get("writable") or [])
    keys.extend(loaded.get("readonly") or [])
    return keys


def balance_delta(tx: dict, address: str) -> int | None:
    """The signed net lamport change for `address` in this transaction.

    `None` when the address is absent from the account list, or when the
    balance arrays are too short to index -- a defensive bounds check against
    a response an untrusted RPC could truncate.
    """
    meta = tx.get("meta") or {}
    pre = meta.get("preBalances") or []
    post = meta.get("postBalances") or []
    keys = account_keys(tx)
    if address not in keys:
        return None
    index = keys.index(address)
    if index >= len(pre) or index >= len(post):
        return None
    return int(post[index]) - int(pre[index])


def _count_credit_instructions(tx: dict, destination: str) -> int:
    """The revisit trigger for D-02 (D-12): how many parsed instructions in
    this transaction credit `destination`, individually.

    Deliberately conservative -- only instructions the RPC itself parsed as a
    native transfer are counted. An unrecognised credit path is undercounted
    here, not overcounted; the day any row shows a count above 1, the dedup
    identity question reopens with a real case attached (see
    `01-DESIGN-NOTE-inflow-identity.md`).
    """
    count = 0
    message = (tx.get("transaction") or {}).get("message") or {}
    instructions = list(message.get("instructions") or [])
    for entry in (tx.get("meta") or {}).get("innerInstructions") or []:
        instructions.extend(entry.get("instructions") or [])
    for instr in instructions:
        if not isinstance(instr, dict):
            continue
        parsed = instr.get("parsed")
        if not isinstance(parsed, dict):
            continue
        if parsed.get("type") not in ("transfer", "transferWithSeed"):
            continue
        info = parsed.get("info") or {}
        if info.get("destination") == destination:
            count += 1
    return count


def fetch_new_signatures(
    rpc,
    address: str,
    until: str | None = None,
    before: str | None = None,
    pages: int = 1,
    limit: int = DEFAULT_PAGE_LIMIT,
) -> tuple[list[dict], bool]:
    """Walk `address`'s signature history backward, at most `pages` pages.

    `before` starts the walk at the OLD end (omit to start from the newest
    signature); `until` bounds the walk at the recent end -- the RPC stops
    once that signature is reached, exclusive.

    Returns `(signatures, exhausted)`, oldest-first. `exhausted` is True only
    when the LAST page fetched came back shorter than `limit` -- the
    unambiguous signal that the address's full history has been walked. A
    walk that merely hits the `pages` budget is not exhausted; the caller
    resumes from the last signature seen next time.
    """
    collected: list[dict] = []
    cursor = before
    exhausted = False
    for _ in range(max(1, pages)):
        page = rpc.signatures_for_address(address, before=cursor, until=until, limit=limit)
        page = page or []
        collected.extend(page)
        if len(page) < limit:
            exhausted = True
            break
        cursor = page[-1]["signature"]
    collected.reverse()
    return collected, exhausted


def scan_inflows(
    rpc,
    evidence,
    mint: str,
    destinations,
    leg_of,
    target: str,
    *,
    until: str | None = None,
    before: str | None = None,
    pages: int = 1,
    limit: int = DEFAULT_PAGE_LIMIT,
):
    """Walk `target`'s signature history and record every configured
    destination it credits.

    One transaction can credit several destinations at once
    (`distribute_creator_fees` pays every shareholder together, D-02), so
    every configured destination present in `account_keys(tx)` with a
    non-zero delta gets its own row from the same signature.

    Returns `(newest_signature_seen, oldest_signature_seen, exhausted)`.
    `exhausted` is only ever True when every fetch in this call succeeded --
    a signature this run could not fetch stops the walk and is never counted
    as history having been fully walked.
    """
    signatures, exhausted = fetch_new_signatures(
        rpc, target, until=until, before=before, pages=pages, limit=limit
    )
    newest_seen: str | None = None
    oldest_seen: str | None = None
    stopped_early = False

    for record in signatures:
        signature = record.get("signature")
        if record.get("err") is not None:
            # A failed transaction's only lamport movement is the fee, debited
            # from accountKeys[0] -- no row, but the signature is
            # definitively not an inflow, so the cursor may pass it.
            newest_seen = signature
            oldest_seen = oldest_seen if oldest_seen is not None else signature
            continue

        tx = rpc.transaction(signature)
        if not tx:
            stopped_early = True
            break
        if (tx.get("meta") or {}).get("err") is not None:
            newest_seen = signature
            oldest_seen = oldest_seen if oldest_seen is not None else signature
            continue

        keys = account_keys(tx)
        block_time = record.get("blockTime") or tx.get("blockTime")
        slot = int(record.get("slot") or tx.get("slot") or 0)
        for destination in destinations:
            if destination not in keys:
                continue
            delta = balance_delta(tx, destination)
            if not delta:
                continue
            evidence.record_inflow(
                signature=signature,
                destination=destination,
                mint=mint,
                leg=leg_of(destination),
                lamports=delta,
                block_time=block_time,
                slot=slot,
                credit_ix_count=_count_credit_instructions(tx, destination),
            )
        newest_seen = signature
        oldest_seen = oldest_seen if oldest_seen is not None else signature

    return newest_seen, oldest_seen, (exhausted and not stopped_early)

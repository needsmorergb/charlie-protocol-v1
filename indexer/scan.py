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

from . import decode
from . import pump
from .rpc import RpcClient

DEFAULT_PAGE_LIMIT = 1000
BACKFILL_PAGES_PER_RUN = 5

# Every mint created through pump's bonding-curve program is minted at a
# fixed 6 decimals -- enforced by the program's own mint initialization, not
# a value `CreateEvent` carries or that varies per coin. This is a protocol
# constant of pump's, unlike `token_total_supply` (EVID-07's figure), which
# is the one quantity this module actually derives rather than assumes.
PUMP_MINT_DECIMALS = 6


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


def _pre_balance(tx: dict, address: str) -> int | None:
    """The raw pre-transaction lamport balance of `address` -- the figure the
    opening-balance mechanism (EVID-02) needs, distinct from `balance_delta`
    which returns the signed net change.
    """
    meta = tx.get("meta") or {}
    pre = meta.get("preBalances") or []
    keys = account_keys(tx)
    if address not in keys:
        return None
    index = keys.index(address)
    if index >= len(pre):
        return None
    return int(pre[index])


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


def _walk_and_record(
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
    """One bounded walk over `target`'s signature history, recording every
    configured destination it credits.

    One transaction can credit several destinations at once
    (`distribute_creator_fees` pays every shareholder together, D-02), so
    every configured destination present in `account_keys(tx)` with a
    non-zero delta gets its own row from the same signature.

    Returns `(newest_signature_seen, oldest_signature_seen, exhausted,
    earliest_fetched)`. `exhausted` is only ever True when every fetch in
    this call succeeded -- a signature this run could not fetch stops the
    walk and is never counted as history having been fully walked.
    `earliest_fetched` is `(signature, pre_balance_for_target)` for the
    oldest transaction this call actually fetched, or `None` -- the input
    the opening-balance mechanism (EVID-02) needs when the walk exhausts.
    """
    signatures, exhausted = fetch_new_signatures(
        rpc, target, until=until, before=before, pages=pages, limit=limit
    )
    newest_seen: str | None = None
    oldest_seen: str | None = None
    earliest_fetched: tuple[str, int | None] | None = None
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

        if earliest_fetched is None:
            earliest_fetched = (signature, _pre_balance(tx, target))

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

    return newest_seen, oldest_seen, (exhausted and not stopped_early), earliest_fetched


def scan_inflows(
    rpc,
    evidence,
    mint: str,
    destinations,
    leg_of,
    target: str,
    *,
    endpoint: str = "*",
    pages: int = BACKFILL_PAGES_PER_RUN,
    limit: int = DEFAULT_PAGE_LIMIT,
    grandfathered=frozenset(),
):
    """The bounded backfill plus the forward catch-up walk, cursor state
    owned by `evidence` (`scan_cursor`, RESEARCH.md Q7 extended with the
    backfill-depth decision).

    Two walks, every call:

    * **Forward** -- from the last-seen signature (if any) up to "now",
      `until=last_signature`. Unbounded by the `pages` budget: new activity
      must never be missed while a backfill is still in progress.
    * **Backward** -- from `oldest_signature` (or the newest signature on a
      first run) for at most `pages` pages. Persists `oldest_signature`
      after every call so the next run resumes where this one stopped, and
      sets `backfill_complete` only when the final page came back shorter
      than `limit` -- the unambiguous signal that history is exhausted.
      Skipped once `backfill_complete` is already set for this endpoint.

    Neither walk advances its cursor past a signature it could not fetch.

    When the backward walk exhausts (history genuinely walked to its start)
    and the earliest transaction it saw shows a non-zero pre-balance for
    `target`, an opening balance is recorded (EVID-02) -- unless `target` is
    in `grandfathered`, whose pre-balance belongs to strangers (D-06).

    Returns `(newest_signature_seen, oldest_signature_seen, backfill_complete)`.
    """
    cursor = evidence.get_cursor(target, "inflow", endpoint=endpoint) or {}
    last_signature = cursor.get("last_signature")
    oldest_signature = cursor.get("oldest_signature")
    backfill_complete = bool(cursor.get("backfill_complete"))

    newest_seen = last_signature
    oldest_seen = oldest_signature

    if last_signature:
        forward_newest, _forward_oldest, _exhausted, _earliest = _walk_and_record(
            rpc, evidence, mint, destinations, leg_of, target,
            until=last_signature, before=None, pages=1_000_000, limit=limit,
        )
        if forward_newest:
            newest_seen = forward_newest

    if not backfill_complete:
        backward_newest, backward_oldest, exhausted, earliest_fetched = _walk_and_record(
            rpc, evidence, mint, destinations, leg_of, target,
            until=None, before=oldest_signature, pages=pages, limit=limit,
        )
        if backward_oldest:
            oldest_seen = backward_oldest
        if not last_signature and backward_newest:
            newest_seen = backward_newest
        backfill_complete = exhausted

        if exhausted and earliest_fetched and target not in grandfathered:
            opening_signature, opening_pre_balance = earliest_fetched
            if opening_pre_balance:
                evidence.record_opening_balance(
                    destination=target,
                    lamports=opening_pre_balance,
                    opening_signature=opening_signature,
                )

    evidence.set_cursor(
        target,
        "inflow",
        endpoint=endpoint,
        last_signature=newest_seen,
        oldest_signature=oldest_seen,
        backfill_complete=int(backfill_complete),
    )
    return newest_seen, oldest_seen, backfill_complete


def scan_inflows_all_endpoints(
    rpc_or_endpoints,
    evidence,
    mint: str,
    destinations,
    leg_of,
    target: str,
    *,
    pages: int = BACKFILL_PAGES_PER_RUN,
    limit: int = DEFAULT_PAGE_LIMIT,
    grandfathered=frozenset(),
    timeout: float = 15.0,
):
    """D-13: the recorded set must not depend on which endpoint answered.

    Measured against mainnet: one default endpoint exposed 2 signatures for
    the seal address, another 77. `RpcClient._pick` always routes a call to
    whichever endpoint is currently healthiest, so a single-endpoint scan's
    recorded set -- and therefore whether `SEAL_BALANCE` passes -- depends on
    which endpoint happened to answer.

    This walks every configured endpoint **independently and deliberately**
    (a fresh single-endpoint `RpcClient` per URL, bypassing `_pick`
    entirely) and unions the results. The union is safe because
    `(signature, destination)` makes re-seeing a transaction idempotent
    (`INSERT OR IGNORE`, never `INSERT OR REPLACE`) -- two endpoints
    independently reporting the same signature write the same row once.

    An endpoint that errors or rate-limits records that fact against ONLY
    that endpoint (`scan_cursor.last_error`) and does not abort the walk of
    the others -- a gap in coverage is stored, never inferred from silence.
    `backfill_complete` for `target` is true once AT LEAST ONE endpoint has
    walked it to a short page (`evidence.is_backfill_complete` already
    implements this "any endpoint" rule).

    `rpc_or_endpoints` accepts three shapes: an `RpcClient` (its configured
    URLs are each given a fresh single-endpoint `RpcClient`, bypassing
    `_pick`); a plain iterable of URL strings (same); or a `dict` mapping an
    endpoint identifier to an already-constructed RPC-like object (what
    `tests/test_scan.py` uses to inject fakes -- this is also what makes an
    identifier other than a real URL, such as a fake test endpoint name,
    equally valid: `scan_cursor.endpoint` is just an opaque identifier).

    Returns `(newest_signature_seen, oldest_signature_seen,
    backfill_complete, contributing_endpoint_count)`.
    """
    if isinstance(rpc_or_endpoints, dict):
        clients = dict(rpc_or_endpoints)
    else:
        urls = (
            rpc_or_endpoints.endpoint_urls
            if isinstance(rpc_or_endpoints, RpcClient)
            else tuple(rpc_or_endpoints)
        )
        clients = {url: RpcClient([url], timeout=timeout) for url in urls}

    for endpoint_id, client in clients.items():
        try:
            scan_inflows(
                client,
                evidence,
                mint,
                destinations,
                leg_of,
                target,
                endpoint=endpoint_id,
                pages=pages,
                limit=limit,
                grandfathered=grandfathered,
            )
        except Exception as exc:
            evidence.set_cursor(
                target, "inflow", endpoint=endpoint_id, last_error=f"{type(exc).__name__}: {exc}"
            )
            continue

    newest_seen: str | None = None
    oldest_seen: str | None = None
    for endpoint_id in clients:
        cursor = evidence.get_cursor(target, "inflow", endpoint=endpoint_id)
        if not cursor:
            continue
        if cursor.get("last_signature") and newest_seen is None:
            newest_seen = cursor["last_signature"]
        if cursor.get("oldest_signature"):
            oldest_seen = cursor["oldest_signature"]

    backfill_complete = evidence.is_backfill_complete(target, "inflow")
    contributing = len(evidence.cursor_endpoints(target, "inflow"))
    return newest_seen, oldest_seen, backfill_complete, contributing


def derive_initial_supply(rpc, evidence, mint: str, *, limit: int = DEFAULT_PAGE_LIMIT):
    """EVID-07: `initial_supply` read out of the mint's own `CreateEvent`,
    never assumed. Cached permanently once found -- RESEARCH.md Q5 measured
    11 round trips (10,163 signatures) to reach $CHARLIE's creation
    transaction, so re-deriving on every tick is not affordable.

    Paginates the **bonding-curve PDA**'s signature history backward, one
    page at a time, oldest-first within each page, looking for a
    `Program data:` line matching `CreateEvent`'s discriminator.

    EVID-08's trigger, settled concretely (RESEARCH.md Open Question 3): the
    derivation is impossible only when the walk exhausts (a page shorter
    than `limit`) without ever finding a `CreateEvent` -- not a page-count
    cap, which would false-negative an actively-traded coin. When that
    happens, a row is written with `raw_supply` null and a reason naming the
    walk that was exhausted. When the walk is merely unfinished (this run
    could not fetch a transaction it needed), NOTHING is written -- a run
    that ran out of budget must not be recorded as a coin whose supply
    cannot be derived; the difference between "not found" and "not finished
    looking" is preserved by returning `None` in that case.
    """
    cached = evidence.initial_supply_for(mint)
    if cached is not None:
        return cached

    target = pump.bonding_curve(mint)
    before = None

    while True:
        page, exhausted = fetch_new_signatures(
            rpc, target, until=None, before=before, pages=1, limit=limit
        )
        for record in page:
            signature = record.get("signature")
            if record.get("err") is not None:
                continue
            tx = rpc.transaction(signature)
            if not tx:
                # This run could not fetch a transaction it needed -- the
                # walk is unfinished, not exhausted. Write nothing.
                return None
            if (tx.get("meta") or {}).get("err") is not None:
                continue
            for payload in decode.program_data_lines(tx):
                event = decode.decode_create_event(payload)
                if event is None:
                    continue
                return evidence.record_initial_supply(
                    mint=mint,
                    raw_supply=event["token_total_supply"],
                    decimals=PUMP_MINT_DECIMALS,
                    creation_signature=signature,
                )

        if exhausted:
            reason = (
                f"walked {target} (the bonding-curve PDA for {mint}) to the end of its "
                "signature history without finding a CreateEvent -- the creation "
                "transaction is unreachable within this endpoint's retained history"
            )
            return evidence.record_initial_supply(
                mint=mint,
                raw_supply=None,
                decimals=PUMP_MINT_DECIMALS,
                unchecked_reason=reason,
            )

        if not page:
            # Defensive: fetch_new_signatures only returns an empty, non-exhausted
            # page if the RPC itself returned nothing without signalling exhaustion.
            return None
        before = page[0]["signature"]  # oldest signature seen this page -- walk further back


def classify_atomicity(tx: dict, mint: str) -> str:
    """RESEARCH.md Q6, EVID-09: `PASS` when a burn for `mint` anywhere in
    this transaction is accompanied, anywhere in the same transaction (not
    necessarily the same parent instruction), by a swap-shaped instruction.
    `FAIL` when a burn is found with no such instruction anywhere in the
    transaction, or when the transaction itself failed
    (`meta.err` is non-null) -- a failed transaction produced no real burn
    and should not have been recorded as one.

    One verdict per transaction: every burn instruction this transaction
    contains for `mint` shares the same answer to "is a swap present
    anywhere in this transaction", so the caller applies this once per
    transaction to every burn row it writes from it.
    """
    if (tx.get("meta") or {}).get("err") is not None:
        return "FAIL"
    if not decode.find_burns(tx, mint):
        return "FAIL"
    return "PASS" if decode.find_swap_shaped(tx) else "FAIL"


def _walk_burns(rpc, evidence, mint: str, *, until=None, before=None, pages=1, limit=DEFAULT_PAGE_LIMIT):
    """One bounded walk over the **mint account's** own signature history,
    recording every burn instruction found against it (D-09: every burn
    against the mint, by anyone -- not one known actor's transactions).

    Returns `(newest_signature_seen, oldest_signature_seen, exhausted)`,
    following `_walk_and_record`'s exact convention: `exhausted` is only
    True when every fetch in this call succeeded, and the cursor never
    advances past a signature this call could not fetch.
    """
    signatures, exhausted = fetch_new_signatures(
        rpc, mint, until=until, before=before, pages=pages, limit=limit
    )
    newest_seen: str | None = None
    oldest_seen: str | None = None
    stopped_early = False

    for record in signatures:
        signature = record.get("signature")
        if record.get("err") is not None:
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

        block_time = record.get("blockTime") or tx.get("blockTime")
        slot = int(record.get("slot") or tx.get("slot") or 0)
        burns = decode.find_burns(tx, mint)
        if burns:
            # At most one boost crank per transaction in practice; if one is
            # present, its SOL-spent figure applies to every burn instruction
            # this same transaction contains.
            boost_event = None
            for payload in decode.program_data_lines(tx):
                event = decode.decode_boost_event(payload)
                if event is not None:
                    boost_event = event
                    break
            # EVID-09: one atomicity verdict per transaction -- every burn
            # this transaction contains for the mint shares the same answer
            # to "is a swap present anywhere in this transaction".
            atomic = classify_atomicity(tx, mint)
            for burn in burns:
                evidence.record_burn_event(
                    signature=signature,
                    mint=mint,
                    instruction_index=burn["instruction_index"],
                    tokens_burned=burn["amount"],
                    sol_spent=boost_event["sol_spent"] if boost_event is not None else None,
                    source="boost_buy_and_burn" if boost_event is not None else "spl_burn",
                    # D-10: unforgeable and trivially checkable -- zero for
                    # every coin until a protocol program id is registered
                    # (phase 5). The correct answer today, not an awkward one.
                    protocol_attributed=0,
                    atomic=atomic,
                    block_time=block_time,
                    slot=slot,
                )
        newest_seen = signature
        oldest_seen = oldest_seen if oldest_seen is not None else signature

    return newest_seen, oldest_seen, (exhausted and not stopped_early)


def scan_burns(
    rpc,
    evidence,
    mint: str,
    *,
    pages: int = BACKFILL_PAGES_PER_RUN,
    limit: int = DEFAULT_PAGE_LIMIT,
):
    """The mint-wide burn scan (EVID-06/D-09): walks the **mint account's**
    own signature history, not the boost vault's -- RESEARCH.md Q8
    recomputed $CHARLIE live and found supply still falling days after the
    boost's 341-second window closed, so a scan restricted to one known
    actor's transactions would permanently under-record.

    Same forward-catch-up-plus-bounded-backfill shape as `scan_inflows`,
    cursor state owned by `evidence` under `purpose='burn'`. Returns
    `(newest_signature_seen, oldest_signature_seen, backfill_complete)`.
    """
    cursor = evidence.get_cursor(mint, "burn") or {}
    last_signature = cursor.get("last_signature")
    oldest_signature = cursor.get("oldest_signature")
    backfill_complete = bool(cursor.get("backfill_complete"))

    newest_seen = last_signature
    oldest_seen = oldest_signature

    if last_signature:
        forward_newest, _forward_oldest, _exhausted = _walk_burns(
            rpc, evidence, mint, until=last_signature, before=None, pages=1_000_000, limit=limit,
        )
        if forward_newest:
            newest_seen = forward_newest

    if not backfill_complete:
        backward_newest, backward_oldest, exhausted = _walk_burns(
            rpc, evidence, mint, until=None, before=oldest_signature, pages=pages, limit=limit,
        )
        if backward_oldest:
            oldest_seen = backward_oldest
        if not last_signature and backward_newest:
            newest_seen = backward_newest
        backfill_complete = exhausted

    evidence.set_cursor(
        mint,
        "burn",
        last_signature=newest_seen,
        oldest_signature=oldest_seen,
        backfill_complete=int(backfill_complete),
    )
    return newest_seen, oldest_seen, backfill_complete

# Build log

Charlie Protocol is built in public. This file is the durable half; the posts
from [@CharlieSlugSOL](https://x.com/CharlieSlugSOL) are the short half.

Entries are append-only. Corrections get a new entry, not an edit — a protocol
about verifiable claims should not quietly rewrite its own record.

---

## 2026-08-29 — the spec goes public before the code exists

The specification has existed for a day. The code that implements it does not
exist at all. Publishing at that ratio is the point: the invariants can be
argued with before anything depends on them.

**What is in this repository.** A spec, a system design, and this log. No
program, no indexer, no site. The status table in the README says so in those
words, because a project whose product is "every number ships with a check that
can fail" cannot open by overstating itself.

**What already runs, elsewhere.** A burn watcher for $CHARLIE, live since
2026-08-23, tracking every SOL inflow to the fee address. It reconciles
`opening + Σ recorded == getBalance` every pass and refuses to post when that
identity breaks. It predates the protocol and implements one leg of it. Roughly
3,500 lines, standard library only — base58, ed25519, PDA derivation and
transaction building are hand-rolled, because `solders` and `PyNaCl` are a
fight on Windows.

**Three things we are not going to pretend about:**

1. **A SOL burn is a lock that can be proven.** Solana has no instruction that reduces SOL supply.
   Every burn dashboard that says otherwise is wrong, and so were we until we
   went looking for the instruction and found it does not exist.

2. **$CHARLIE cannot enroll in its own protocol.** Its sharing config is
   `admin_revoked` — permanent, single shareholder, vanity address. Only pump
   can reset it. The reference implementation is the coin the protocol helps
   least.

3. **BOOST already did the only real burn $CHARLIE has ever had.** At migration
   on 2026-08-21, pump's boost vault took 17.584506 SOL and ran 29
   `boost_buy_and_burn` cranks over 341 seconds, destroying 43,575,480 tokens.
   That is all but ~34,700 of every $CHARLIE ever burned. It was over in under
   six minutes, it ran on pump's key, and our own watcher could not see it —
   it counts native SOL inflows, and this was an SPL burn on the AMM side.
   A watcher that misses the largest event in the coin's history is a lesson,
   not a footnote.

**Next.** The indexer, because it is the part that can be wrong in public and
therefore the part worth building first. Read a sharing config, emit
`{ sol_burn, burn, paid }` in bps, store the observation append-only including the
failures.

**Open, unresolved.** Whether pump will crank a funded boost vault on a
cadence. Permissionless funding is proven; `boost_buy_and_burn` requires pump's
boost authority to sign. Without an answer, the BURN leg has to run through our
own program.

---

## 2026-08-29 — the indexer runs, and the first check it ran failed

The indexer reads a pump sharing config, attributes every shareholder to
a leg, recomputes what can be recomputed, and appends the result — pass or
fail — to a JSONL log. Standard library only, 36 offline tests, no install
step. Pointed at $CHARLIE it confirms the config exactly as described here
yesterday: `admin_revoked`, one shareholder, `burn111…111` at 10000 bps.

Then it failed a check on our own coin.

**`burn111…111` is not program-derived.** It is a vanity address, not the PDA
§3 requires. Verified three ways before believing it: PDAs decompress one way,
ordinary account addresses decompress the other, and random 32-byte values split
about 49/51 — which is the number the maths predicts, so the derivation test is
right and the address really is an ordinary one.

PROTOCOL.md §3 grandfathers that address, but only for *attribution* — the
`==` invariant degrading to `≤` because the vault is shared. This is a second,
separate weakness the spec did not name: the vault does not meet the derivation
standard. Its standing rests on convention, which is the exact thing §3 was
written to require an alternative to. So `SOL_BURN_UNSPENDABLE` fails, and by the
silence rule the protocol may not publish a SOL burn total for $CHARLIE.
178.734302038 SOL sits there as of today and the indexer will report the balance
as an observation while refusing to call it a burn.

By way of contrast, `1nc1nerator11111111111111111111111111111111` **is**
program-derived. A burn address that meets the standard was always possible.
Ours is not one.

**`UNCHECKED` blocks publication as hard as `FAIL`.** Four of the seven checks
cannot be computed yet — inflows are not recorded per signature and burn events
are not recorded at all. The tempting shortcut is to mark those green, or
silent, and publish the figures anyway. They are marked `UNCHECKED` and they
withhold their figures, so today the indexer will publish a coin's split and
nothing else, naming the check that stopped each of the other three. "We do not
know whether this number is right" and "this number is wrong" read identically
to someone downstream of the post.

**A new invariant, found by building: `BURN_IRREVERSIBLE`.** A live mint
authority can reissue every token a crank ever burned. "Permanently destroyed"
is then false regardless of how honest the burn arithmetic is. It is not in
PROTOCOL.md §4; it should be. $CHARLIE passes it — mint and freeze authority
are both revoked.

**And an arithmetic discrepancy we are not going to smooth over.** $CHARLIE's
supply reads 956,384,474.035955 today. Against pump's standard 1,000,000,000
that is 43,615,525.96 destroyed. Yesterday's entry accounted for
43,575,480 + ~34,700 ≈ 43,610,180. About 5,346 tokens are unexplained. The gap
is small and the "~34,700" was approximate, so it is probably nothing — but
"probably nothing" is precisely why `BURN_SUPPLY` is `UNCHECKED` rather than
green. Nothing in the system currently records which burns produced that
supply, and a supply reading alone cannot tell you.

**Next.** Recording inflows per signature, which turns `SOL_BURN_BALANCE` and
`OPS_ROUTED` from `UNCHECKED` into checks that can actually fail. Then burn
events, which does the same for `BURN_SUPPLY`.

**Open, unresolved.** Whether the spec should let a grandfathered on-curve
address ever carry a SOL burn total, or whether $CHARLIE's own figure stays
unpublishable until pump resets the config. The code currently says
unpublishable, which is the harder answer and probably the right one.

---

## 2026-09-03 — the loop, and three changes that lived only in the output repository

Three commits landed overnight in `charlie-protocol-site`, the repository
whose README says it holds output only and that nothing in it is
hand-written: the counterfactual came off the coin page, a burn to a burn
address became a burn, and the landing page gained the loop. Each one
changed `indexer/site.py` or `indexer/invariants.py` in that repository and
regenerated the pages there. This repository, the source, had none of them.
508 tests here, none of them over any of it.

That is the failure the site README warns about, run backwards: not a hand
edit to the page, but a change to the generator in the place that is
supposed to receive the generator. Today the source is here, and every one
of the three ships with a check that can fail. 534 tests.

**A burn to a burn address is a burn.** `SOL_BURN_UNSPENDABLE` failed
$CHARLIE for four days because `burn111…111` is on the curve and PROTOCOL.md
section 3 asks for a program-derived vault. That standard is written for a
coin enrolled in the protocol, and $CHARLIE is not enrolled and cannot be.
Grading it against a requirement it was never offered was a category error,
and the red FAIL on the reference coin's own page was its output. The check
now passes a recognised burn address and the incinerator, and fails an
ordinary address someone can spend from. PROTOCOL.md section 3 and the
README said the old thing; both say this now.

What still withholds $CHARLIE's SOL burn total is `SOL_BURN_BALANCE`: the
address is shared, so attribution across the coins using it is not possible,
and it carries the weaker `<=` invariant. That has not changed. What has is
that the withholding names the right check.

**Two things found by writing the tests.** First: `legs.py` puts only a
recognised burn address, the incinerator or a derived vault on the sol_burn
leg, so on live data with the default registry `SOL_BURN_UNSPENDABLE` can now
read PASS or UNCHECKED and never FAIL. It is a guard against attribution
changing under it, not a check that can fail a real coin today, and the test
that reaches its FAIL branch says so in its docstring rather than pretending
otherwise. Second: an evidence store that had completed a walk and recorded
no inflow used to pass `0 <= balance` vacuously and publish a total of
0 lamports, which reads as "this coin burned nothing". It is UNCHECKED now.
Nothing measured is not a measurement of nothing.

**The counterfactual is off the coin page.** It stated what a coin's burns
would have destroyed had the SOL gone to a burn address instead of buying
tokens. Nothing happened for a check to read, so no check backed it, and it
was the largest number on the page, printed under "This did not happen".
The function still exists and its own tests still hold it to its contract;
`render()` no longer calls it, and a test reads the source to say so.

**The loop.** The landing page now leads with the mechanism, above the
counters: trading pays a creator fee, the fee buys the token and the token
is burned, buying is volume and volume pays the next fee, a share reaches
the incinerator. A ring turns because a still picture of a cycle is a
picture of something that has stopped; the ordered list under it is the
mechanism, readable with the animation off, images off, or a screen reader
running, and `prefers-reduced-motion` stops all of it. The section takes no
observation and states no number: it is what the protocol does, not
something the chain was read for, so there is nothing in it for a check to
gate.

**One more line that stopped being true.** The coin page's Risks list said
"`SOL_BURN_UNSPENDABLE` fails permanently for this coin, not pending" as a
template constant. Written for $CHARLIE, rendered on every coin's page, false
for every other coin from the start, and false for $CHARLIE from the moment
the check passed it. It is now read from the coin's own check, the way the
burn-walk risk already was, in four branches.

**What is not done, stated plainly.** The deployed $CHARLIE page still
carries that sentence. The intake workflow re-stamps four static pages and
regenerates nothing else, and the environment this was built in cannot reach
the RPC gateway, so the coin page and `index.html` regenerate on the next
local run of the `site` subcommand, not before. The page is committed
output; it was not hand-edited to say the right thing sooner.

---

## 2026-09-03 — the check reads the registry it was given

The entry above left one thing open and it did not survive the afternoon.
`SOL_BURN_UNSPENDABLE` decided what counted as a recognised burn address by
reading a default `Registry()` inside the check, not the registry the split
had been attributed under. Two consequences, both bad. On live data the
check could read PASS or UNCHECKED and never FAIL, because `legs.py` only
puts a grandfathered address, the incinerator or a derived vault on the
sol_burn leg, and the default registry recognised every one of those. And
the test that reached its FAIL branch did so only through a mismatch: a
fixture registry that grandfathered a wallet, graded by a default registry
that did not. A check whose only failing path is a disagreement between two
registries is not a check, it is an accident.

**The registry now has two sets, and they are different questions.**
`grandfathered_sol_burn` is attribution: this address goes on the sol_burn
leg because a coin routes there calling it a burn. `recognised_burn` is
recognition: the chain treats this address as a burn, SOL sent there is out
of circulation and stays there. They hold the same one address today and are
kept apart on purpose, because an address attributed to the leg without
being recognised is precisely the case the check exists to catch. The check
takes the registry it was observed under and asks it, and `observe()` passes
that registry through. The same wallet under a registry that attributes it
but does not recognise it fails; under one that does both, it passes. Two
tests, one split, and the difference between them is the registry alone.

The coin page's SOL burn risk line, which had picked up the same
default-registry read yesterday, now reads the attribution the observation
already carries. The page states the standing the coin was observed under,
which a registry consulted at render time is not.

**Unchanged, and worth saying.** $CHARLIE still passes: `burn111…111` is in
both sets. What withholds its SOL burn total is still `SOL_BURN_BALANCE`,
for the same reason as before.

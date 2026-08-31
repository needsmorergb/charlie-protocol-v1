# web/

Four routes, described in [`ARCHITECTURE.md`](../ARCHITECTURE.md) §4. The
single-coin page is phase 2; the index, `/enroll` and `/verify` are phases 3
and 5.

```
/                 the index — every coin, its split, its class
/coin/<mint>      one coin: live checks, totals, links to the proof
/enroll           derive the two PDAs, emit the exact bps config to set
/verify/<mint>    paste a mint, get a shareable verdict
```

The rule that separates this from every burn dashboard: every number displays
the check that backs it, and a failed check renders **louder** than a passing
one. A dashboard that can only report good news is decoration.

## What is built today

One of the four routes — `/coin/<mint>`, for exactly one coin — is generated
and committed: [`8FhAXv2tfXUpyMbJsHDHX9zfiEb9PERzFWSY9sgLpump.html`](./8FhAXv2tfXUpyMbJsHDHX9zfiEb9PERzFWSY9sgLpump.html)
and its sibling raw record,
[`8FhAXv2tfXUpyMbJsHDHX9zfiEb9PERzFWSY9sgLpump.json`](./8FhAXv2tfXUpyMbJsHDHX9zfiEb9PERzFWSY9sgLpump.json)
(the exact `publish.durable_record()` the page was generated from — WEB-06).
The other three routes (the index, `/enroll`, `/verify`) are phases 3 and 5;
built today, every page for them would read `UNCHECKED`, which is why the
single-coin page is first.

Both files are committed build artifacts, produced by, and only by, the
`site` subcommand:

```
python -m indexer site 8FhAXv2tfXUpyMbJsHDHX9zfiEb9PERzFWSY9sgLpump --write --out web
```

Never hand-edit either file. A hand edit makes the committed page and the
generator that produced it disagree, which is precisely the failure mode this
artifact exists to make visible.

## Regeneration overwrites in place (D-19)

Running the command above again overwrites the same two paths — there is no
dated series and no `latest` pointer. This is safe *because* the artifacts
are committed: nothing is lost by overwriting them, since every prior version
is already in git history. Recover any earlier version, or find the exact
commit at which a figure moved from published to withheld, with:

```
git log -p web/8FhAXv2tfXUpyMbJsHDHX9zfiEb9PERzFWSY9sgLpump.html
```

The page itself renders this same command in its closing "Raw Observation
JSON" section, against its own filename, so the page and this README never
disagree about where its history lives.

## What is deliberately out of scope (D-15)

Hosting, CI, a deploy workflow, and a public URL are not part of this phase.
None of Phase 2's five success criteria requires a URL — they require that a
visitor *seeing* the page sees no figure that no passing check backs, which
is a property of the generated file, testable offline. Committing the
artifact also means the rendered page shows up in a diff and can be reviewed
like code, which matters more for a page whose whole claim is falsifiability
than a live URL does. Hosting stays cheap to add later and nothing about
deferring it here is one-way.

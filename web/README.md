# web/ — not built

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

Built today, every page would read `UNCHECKED`. That is why it is third.

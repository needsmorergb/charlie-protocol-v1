# web/ — not built

Four routes, described in `ARCHITECTURE.md` §4 and planned in
[`../ROADMAP.md`](../ROADMAP.md) §4.

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

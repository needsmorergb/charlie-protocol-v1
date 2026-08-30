"""Append-only observation log.

ARCHITECTURE.md sec.3: "Failures are stored, not discarded. A red state has to
be as durable and as linkable as a green one, or the index is marketing."

So the store has exactly one write operation, `append`. There is no update and
no delete, and that is a design decision rather than an unfinished feature: a
protocol whose subject is verifiable claims cannot quietly rewrite its own
record. A wrong observation is corrected by a later observation, the way
BUILDLOG.md corrects itself with a new entry rather than an edit.

One JSON object per line. Line-oriented so it can be tailed, diffed, appended
to concurrently, and read by anything.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_PATH = Path("state") / "observations.jsonl"


class Store:
    def __init__(self, path=DEFAULT_PATH):
        self.path = Path(path)

    def append(self, observation) -> dict:
        record = observation.as_dict() if hasattr(observation, "as_dict") else observation
        line = json.dumps(record, separators=(",", ":"), sort_keys=True)
        if "\n" in line:  # pragma: no cover - json.dumps escapes newlines
            raise ValueError("a record must serialise to a single line")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Append mode plus an explicit flush+fsync: a tick that reported a
        # failure and then lost the record to a buffer is the one case the log
        # exists to prevent.
        with open(self.path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return record

    def read(self, mint: str | None = None, limit: int | None = None) -> list[dict]:
        if not self.path.exists():
            return []
        records = []
        with open(self.path, encoding="utf-8") as handle:
            for number, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    # A corrupt line is reported as a record, not skipped in
                    # silence. Silently dropping it would make the log look
                    # complete when it is not.
                    record = {"schema": 0, "corrupt": True, "line": number, "raw": line}
                if mint and record.get("mint") != mint:
                    continue
                records.append(record)
        return records[-limit:] if limit else records

    def latest(self, mint: str) -> dict | None:
        records = self.read(mint=mint, limit=1)
        return records[0] if records else None

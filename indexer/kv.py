"""A small key-value store shared by the claim page and the tag bot.

Upstash Redis over its REST API: POST the command as a JSON array to the
database URL with the REST token as a bearer, and read `result` back. Values
are JSON strings. `MemoryKV` is an in-process stand-in with the same methods,
for tests.

The keys are the only contract between the site and the bot:

* `claim:credit:{xid}`  what a requester may claim (the bot writes it)
* `claim:queue`         claims waiting to be paid (the site pushes, the bot pops)
* `claim:status:{xid}`  the latest claim's state (the site writes "queued",
                        the bot writes the rest)
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

TIMEOUT = 10


class KVError(RuntimeError):
    """The store answered an error, or did not answer."""


class KV:
    def __init__(self, url: str, token: str, *, opener=None):
        self.url = (url or "").rstrip("/")
        self.token = token
        self.opener = opener

    def command(self, *args) -> object:
        """Run one Redis command, e.g. `command("SET", "k", "v", "EX", 60)`."""
        if not self.url or not self.token:
            raise KVError("the store is not configured")
        body = json.dumps([str(a) if not isinstance(a, str) else a for a in args]).encode()
        request = urllib.request.Request(self.url, body, headers={
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }, method="POST")
        open_ = self.opener or urllib.request.urlopen
        try:
            with open_(request, timeout=TIMEOUT) as response:
                raw = response.read(2_000_000)
        except urllib.error.HTTPError as exc:
            raise KVError(f"the store answered HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError) as exc:
            raise KVError(f"the store did not answer: {exc}") from None
        try:
            answer = json.loads(raw.decode("utf-8"))
        except ValueError:
            raise KVError("the store answered something that is not JSON") from None
        if not isinstance(answer, dict):
            raise KVError("the store answered an unexpected shape")
        if answer.get("error"):
            raise KVError(f"the store refused: {answer['error']}")
        return answer.get("result")

    # -- JSON values --

    def get_json(self, key) -> object | None:
        return _loads(self.command("GET", key))

    def set_json(self, key, value, *, ex: int | None = None) -> None:
        args = ["SET", key, json.dumps(value, separators=(",", ":"))]
        if ex is not None:
            args += ["EX", int(ex)]
        self.command(*args)

    def push(self, key, value) -> None:
        """Append to the list at `key` (RPUSH)."""
        self.command("RPUSH", key, json.dumps(value, separators=(",", ":")))

    def pop(self, key) -> object | None:
        """Take from the front of the list at `key` (LPOP); None when empty."""
        return _loads(self.command("LPOP", key))

    def delete(self, key) -> None:
        self.command("DEL", key)


def _loads(raw) -> object | None:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


class MemoryKV(KV):
    """An in-process store with the same methods, for tests. Supports GET,
    SET (with EX), DEL, RPUSH, LPOP, LLEN and LRANGE."""

    def __init__(self, *, now=time.time):
        super().__init__("memory://", "memory")
        self.now = now
        self.values: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}
        self.expires: dict[str, float] = {}
        self.log: list[tuple] = []

    def _expire(self, key: str) -> None:
        at = self.expires.get(key)
        if at is not None and self.now() >= at:
            self.values.pop(key, None)
            self.expires.pop(key, None)

    def command(self, *args) -> object:
        self.log.append(args)
        if not args:
            raise KVError("empty command")
        name = str(args[0]).upper()
        key = str(args[1]) if len(args) > 1 else ""
        self._expire(key)
        if name == "GET":
            return self.values.get(key)
        if name == "SET":
            self.values[key] = str(args[2])
            self.expires.pop(key, None)
            rest = [str(a).upper() for a in args[3:]]
            if "EX" in rest:
                self.expires[key] = self.now() + int(args[3 + rest.index("EX") + 1])
            return "OK"
        if name == "DEL":
            found = key in self.values or key in self.lists
            self.values.pop(key, None)
            self.lists.pop(key, None)
            return int(found)
        if name == "RPUSH":
            items = self.lists.setdefault(key, [])
            items.extend(str(a) for a in args[2:])
            return len(items)
        if name == "LPOP":
            items = self.lists.get(key) or []
            return items.pop(0) if items else None
        if name == "LLEN":
            return len(self.lists.get(key) or [])
        if name == "LRANGE":
            items = self.lists.get(key) or []
            start, stop = int(args[2]), int(args[3])
            stop = len(items) if stop == -1 else stop + 1
            return items[start:stop]
        raise KVError(f"MemoryKV does not implement {name}")

"""Minimal multi-endpoint Solana JSON-RPC client (stdlib only).

Small, but it carries the failover behaviour a long-running indexer needs:

* every request has a hard timeout -- a hung provider must not wedge a tick;
* network errors, timeouts and HTTP 5xx rotate to the next endpoint;
* HTTP 429 backs an endpoint off but keeps it in rotation;
* JSON-RPC *application* errors are raised to the caller and never counted
  against the endpoint -- the provider worked, our request was wrong.

The distinction in that last line matters more here than in a bot. An indexer
that silently treats "the node refused" the same as "the account does not
exist" will publish a green state it never actually computed.
"""

from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

USER_AGENT = "charlie-protocol-indexer/0.1"

DEFAULT_ENDPOINTS = (
    "https://solana-rpc.publicnode.com",
    "https://api.mainnet-beta.solana.com",
    "https://solana.drpc.org",
)


class RpcError(RuntimeError):
    """A JSON-RPC application error: the node answered, it just said no."""

    def __init__(self, code: int, message: str, method: str):
        super().__init__(f"{method}: [{code}] {message}")
        self.code = code
        self.message = message
        self.method = method


class RpcUnavailable(RuntimeError):
    """Every endpoint failed at the infrastructure level."""


@dataclass
class _Infra(Exception):
    backoff: float = 2.0


@dataclass
class _Endpoint:
    url: str
    failures: int = 0
    cooldown_until: float = 0.0

    def available(self, now: float) -> bool:
        return now >= self.cooldown_until

    def penalise(self, now: float, seconds: float) -> None:
        self.failures += 1
        delay = min(seconds * (2 ** min(self.failures - 1, 4)), 120.0)
        self.cooldown_until = now + delay * (0.75 + random.random() * 0.5)

    def reward(self) -> None:
        self.failures = 0
        self.cooldown_until = 0.0


class RpcClient:
    def __init__(
        self,
        endpoints=DEFAULT_ENDPOINTS,
        timeout: float = 15.0,
        max_retries: int = 3,
        sleep=time.sleep,
    ):
        if not endpoints:
            raise ValueError("at least one RPC endpoint is required")
        self._endpoints = [_Endpoint(url) for url in endpoints]
        self._timeout = timeout
        self._max_retries = max(1, max_retries)
        self._sleep = sleep
        self._id = 0
        self.calls = 0

    @property
    def endpoint_urls(self) -> tuple[str, ...]:
        """This client's configured endpoint URLs, in the order given.

        Exists for `scan.scan_inflows_all_endpoints` (D-13): a per-endpoint
        walk needs to address each endpoint deliberately rather than let
        `_pick` choose the healthiest one, which is exactly what makes the
        recorded set non-deterministic across runs.
        """
        return tuple(e.url for e in self._endpoints)

    # -- public -----------------------------------------------------------
    def call(self, method: str, params: list | None = None):
        self._id += 1
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or []}
        ).encode("utf-8")

        last_error: Exception | None = None
        for _ in range(self._max_retries * len(self._endpoints)):
            endpoint = self._pick()
            if endpoint is None:
                wait = max(0.0, min(e.cooldown_until for e in self._endpoints) - time.time())
                self._sleep(min(wait, 5.0) or 0.25)
                continue
            try:
                body = self._post(endpoint.url, payload)
            except _Infra as exc:
                last_error = exc.__cause__ or exc
                endpoint.penalise(time.time(), exc.backoff)
                continue

            endpoint.reward()
            self.calls += 1
            if body.get("error"):
                err = body["error"]
                raise RpcError(err.get("code", 0), str(err.get("message", "")), method)
            return body.get("result")

        raise RpcUnavailable(f"{method}: all RPC endpoints failed ({last_error})")

    def program_accounts(
        self,
        program_id: str,
        *,
        filters=(),
        data_slice: dict | None = None,
        encoding: str = "base64",
    ) -> list[dict]:
        """`getProgramAccounts`, typed: every `{pubkey, account}` entry the
        node returns for `program_id`, normalised to a list -- never `None`,
        the same defensive posture `accounts()` already takes against a
        short response.

        `filters` and `data_slice` pass straight through to the RPC;
        `data_slice` is omitted from the params entirely when not given (a
        full-data sweep of the fee-sharing program is a ~965 MB response at
        today's ~603K-account scale -- RESEARCH.md's Pitfall 3 -- so a caller
        narrowing the payload is deliberate, never implied by a default).

        Delegates to `call()` -- the retry loop, the endpoint rotation and
        the `RpcError`-vs-`RpcUnavailable` distinction it already implements
        apply here unchanged. This method adds a typed shape, not a second
        transport: research verified that a `getProgramAccounts` refusal from
        an endpoint that does not support it already arrives as a non-2xx
        HTTP status, already treated by `_post()` as an infrastructure
        failure that rotates to the next endpoint.
        """
        options: dict = {"encoding": encoding}
        if filters:
            options["filters"] = list(filters)
        if data_slice is not None:
            options["dataSlice"] = data_slice
        result = self.call("getProgramAccounts", [program_id, options])
        return list(result or [])

    def accounts(self, addresses: list[str]) -> list[dict | None]:
        """Always exactly `len(addresses)` entries, whatever the node returns.

        A provider answering with a short list must not become an IndexError
        three frames later in a decoder.
        """
        result = self.call("getMultipleAccounts", [addresses, {"encoding": "base64"}])
        values = list((result or {}).get("value") or [])
        values.extend([None] * (len(addresses) - len(values)))
        return values[: len(addresses)]

    def balance(self, address: str) -> int:
        result = self.call("getBalance", [address])
        return int((result or {}).get("value") or 0)

    def signatures_for_address(
        self,
        address: str,
        before: str | None = None,
        until: str | None = None,
        limit: int = 1000,
    ) -> list[dict]:
        """Newest-first signature history for `address`.

        `before` walks strictly older than that signature (exclusive); `until`
        stops the walk once that signature is reached (exclusive). Both are
        the RPC's own semantics -- this method does not reinterpret them.
        """
        options: dict = {"limit": limit}
        if before:
            options["before"] = before
        if until:
            options["until"] = until
        result = self.call("getSignaturesForAddress", [address, options])
        return list(result or [])

    def transaction(self, signature: str) -> dict | None:
        """A single transaction, fully jsonParsed.

        `maxSupportedTransactionVersion: 0` is required or the RPC refuses any
        versioned transaction outright; `commitment: confirmed` matches the
        rest of this client.
        """
        return self.call(
            "getTransaction",
            [
                signature,
                {
                    "encoding": "jsonParsed",
                    "maxSupportedTransactionVersion": 0,
                    "commitment": "confirmed",
                },
            ],
        )

    # -- internals --------------------------------------------------------
    def _pick(self) -> _Endpoint | None:
        now = time.time()
        healthy = [e for e in self._endpoints if e.available(now)]
        if not healthy:
            return None
        return min(healthy, key=lambda e: e.failures)

    def _post(self, url: str, payload: bytes) -> dict:
        request = urllib.request.Request(
            url,
            data=payload,
            headers={
                "content-type": "application/json",
                "accept": "application/json",
                "user-agent": USER_AGENT,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise _Infra(backoff=5.0) from exc
            if exc.code >= 500:
                raise _Infra(backoff=2.0) from exc
            raise _Infra(backoff=30.0) from exc
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise _Infra(backoff=2.0) from exc

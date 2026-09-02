"""Offline tests for `indexer/coverage.py` and the pieces Task 1 of
03-01-PLAN.md factors out of `indexer/pump.py`, `indexer/legs.py`,
`indexer/evidence.py` and `indexer/publish.py` to support it: the
`RpcClient.program_accounts` wrapper, the single guarded
`decode_sharing_config`, `legs.classify_split`, the `sharing_config` table,
and `publish.classification`.

`python -m unittest discover -s tests -t tests -p "test_coverage.py"`.

No network. Every account fed to a decoder is built byte by byte, in the
style of `tests/test_indexer.py`'s `config_account()` -- a pump layout
change must show up as a failing decode test, never as a wrong published
number. The two negative decodes (wrong owner, wrong discriminator) and the
truncated-versus-malformed pair are the acceptance criteria this task exists
to prove.
"""

from __future__ import annotations

import base64
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from indexer import coverage, invariants, publish
from indexer.base58 import encode, pubkey_bytes
from indexer.evidence import Evidence
from indexer.legs import Registry, Split, classify_split, split_of
from indexer.pump import (
    DISC_BONDING_CURVE,
    DISC_SHARING_CONFIG,
    PUMP_FEE_SHARE_PROGRAM,
    PUMP_PROGRAM,
    SHARING_CONFIG_ACCOUNT_BYTES,
    SHARING_CONFIG_DISCRIMINATOR_B58,
    SINGLE_SHAREHOLDER_SLICE,
    DecodeError,
    SharingConfig,
    TruncatedConfig,
    decode_sharing_config,
    read_sharing_config,
)
from indexer.rpc import RpcClient, RpcError, RpcUnavailable

ADMIN = "2CFywHXDPjDK2iRQsb95vnjgncDUZeQKJ6MceJ4ALpdc"
MINT_A = "8FhAXv2tfXUpyMbJsHDHX9zfiEb9PERzFWSY9sgLpump"
MINT_B = "So11111111111111111111111111111111111111112"
ADDR_1 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ADDR_2 = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
ADDR_3 = "burn111111111111111111111111111111111111111"
CONFIG_ADDR = "8cUvP3q3KqcKMT6rEowN55ZepafYLFLwY2vijETRK3E4"


# -- fixtures ---------------------------------------------------------------
def full_config_bytes(mint, holders, admin_revoked=True, admin=ADMIN) -> bytes:
    """A complete, 1024-byte SharingConfig account body -- pump pre-allocates
    the whole account regardless of shareholder count (measured live).
    """
    data = bytearray(DISC_SHARING_CONFIG)
    data += bytes([255, 2, 1])  # bump, version, status
    data += pubkey_bytes(mint)
    data += pubkey_bytes(admin)
    data += bytes([1 if admin_revoked else 0])
    data += len(holders).to_bytes(4, "little")
    for address, bps in holders:
        data += pubkey_bytes(address) + bps.to_bytes(2, "little")
    data += bytes(SHARING_CONFIG_ACCOUNT_BYTES - len(data))
    return bytes(data)


def account_dict(data: bytes, owner: str, space: int | None = None) -> dict:
    """The `{owner, data, space}` shape both `getMultipleAccounts` and
    `getProgramAccounts` nest a result under. `space` defaults to the data
    actually in hand -- an unsliced read, where the account's own recorded
    size and the bytes returned agree.
    """
    return {
        "owner": owner,
        "data": [base64.b64encode(data).decode(), "base64"],
        "space": space if space is not None else len(data),
    }


def program_account_entry(pubkey: str, account: dict) -> dict:
    return {"pubkey": pubkey, "account": account}


class FakeProgramAccountsRpc:
    """A dedicated-enough stand-in for `RpcClient.program_accounts` --
    `coverage.sweep` only ever calls this one method.
    """

    def __init__(self, entries):
        self._entries = entries
        self.calls = []

    def program_accounts(self, program_id, *, filters=(), data_slice=None, encoding="base64"):
        self.calls.append({"program_id": program_id, "filters": list(filters), "data_slice": data_slice})
        return self._entries


# -- RpcClient.program_accounts ----------------------------------------------
class TestProgramAccountsWrapper(unittest.TestCase):
    def _client_with_stub(self, result):
        client = RpcClient(endpoints=("http://x",))
        captured = {}

        def fake_call(method, params=None):
            captured["method"] = method
            captured["params"] = params
            return result

        client.call = fake_call
        return client, captured

    def test_null_result_normalises_to_empty_list_never_none(self):
        client, captured = self._client_with_stub(None)
        result = client.program_accounts("PROG")
        self.assertEqual(result, [])
        self.assertEqual(captured["method"], "getProgramAccounts")

    def test_filters_and_data_slice_pass_through_unchanged(self):
        filters = [{"memcmp": {"offset": 0, "bytes": "abc"}}]
        data_slice = {"offset": 0, "length": 114}
        client, captured = self._client_with_stub([{"pubkey": "p", "account": {}}])
        result = client.program_accounts("PROG", filters=filters, data_slice=data_slice)
        self.assertEqual(result, [{"pubkey": "p", "account": {}}])
        params = captured["params"]
        self.assertEqual(params[0], "PROG")
        self.assertEqual(params[1]["filters"], filters)
        self.assertEqual(params[1]["dataSlice"], data_slice)

    def test_data_slice_omitted_from_params_when_not_given(self):
        client, captured = self._client_with_stub([])
        client.program_accounts("PROG")
        self.assertNotIn("dataSlice", captured["params"][1])

    def test_rpc_error_and_rpc_unavailable_remain_distinguishable(self):
        client = RpcClient(endpoints=("http://x",))

        def raise_rpc_error(method, params=None):
            raise RpcError(1, "no", method)

        client.call = raise_rpc_error
        with self.assertRaises(RpcError):
            client.program_accounts("PROG")

        def raise_unavailable(method, params=None):
            raise RpcUnavailable("down")

        client.call = raise_unavailable
        with self.assertRaises(RpcUnavailable):
            client.program_accounts("PROG")


# -- pump.decode_sharing_config / pump.TruncatedConfig -----------------------
class TestDecodeSharingConfig(unittest.TestCase):
    def test_discriminator_b58_is_computed_not_pasted(self):
        self.assertEqual(SHARING_CONFIG_DISCRIMINATOR_B58, encode(DISC_SHARING_CONFIG))

    def test_valid_account_decodes_matching_fields(self):
        data = full_config_bytes(MINT_A, [(ADDR_1, 6_000), (ADDR_2, 4_000)], admin_revoked=False)
        account = account_dict(data, PUMP_FEE_SHARE_PROGRAM)
        config = decode_sharing_config(CONFIG_ADDR, account)
        self.assertEqual(config.address, CONFIG_ADDR)
        self.assertEqual(config.mint, MINT_A)
        self.assertFalse(config.admin_revoked)
        self.assertEqual(config.admin, ADMIN)
        self.assertEqual(config.shareholders, ((ADDR_1, 6_000), (ADDR_2, 4_000)))

    def test_wrong_owner_raises_decode_error_naming_the_owner(self):
        """The filter matching is not sufficient -- the memcmp match is a
        byte comparison at an offset, never proof of account type."""
        data = full_config_bytes(MINT_A, [(ADDR_1, 10_000)])
        account = account_dict(data, PUMP_PROGRAM)  # a valid body, wrong owner
        with self.assertRaises(DecodeError) as caught:
            decode_sharing_config(CONFIG_ADDR, account)
        self.assertIn(PUMP_PROGRAM, str(caught.exception))
        self.assertIn("owned by", str(caught.exception))

    def test_wrong_discriminator_raises_decode_error_with_otherwise_valid_body(self):
        data = bytearray(full_config_bytes(MINT_A, [(ADDR_1, 10_000)]))
        data[0:8] = DISC_BONDING_CURVE  # otherwise-valid body, wrong disc
        account = account_dict(bytes(data), PUMP_FEE_SHARE_PROGRAM)
        with self.assertRaises(DecodeError):
            decode_sharing_config(CONFIG_ADDR, account)

    def test_truncated_slice_of_a_real_multi_shareholder_config_raises_truncated_config(self):
        holders = [(ADDR_1, 3_000), (ADDR_2, 3_000), (ADDR_3, 4_000)]
        full = full_config_bytes(MINT_A, holders)
        sliced = full[:SINGLE_SHAREHOLDER_SLICE]
        account = account_dict(sliced, PUMP_FEE_SHARE_PROGRAM, space=SHARING_CONFIG_ACCOUNT_BYTES)
        with self.assertRaises(TruncatedConfig) as caught:
            decode_sharing_config(CONFIG_ADDR, account)
        self.assertEqual(caught.exception.declared_count, 3)
        self.assertEqual(caught.exception.address, CONFIG_ADDR)
        self.assertEqual(caught.exception.mint, MINT_A)

    def test_same_truncated_bytes_with_space_equal_to_data_length_raises_decode_error_not_truncated(self):
        holders = [(ADDR_1, 3_000), (ADDR_2, 3_000), (ADDR_3, 4_000)]
        full = full_config_bytes(MINT_A, holders)
        sliced = full[:SINGLE_SHAREHOLDER_SLICE]
        account = account_dict(sliced, PUMP_FEE_SHARE_PROGRAM, space=len(sliced))
        with self.assertRaises(DecodeError) as caught:
            decode_sharing_config(CONFIG_ADDR, account)
        self.assertNotIsInstance(caught.exception, TruncatedConfig)

    def test_unsliced_account_declaring_more_shareholders_than_it_holds_is_a_plain_decode_error(self):
        """A genuine layout violation and a deliberate truncation must not
        read the same."""
        data = bytearray(DISC_SHARING_CONFIG + bytes([255, 2, 1]))
        data += pubkey_bytes(MINT_A) + pubkey_bytes(ADMIN) + bytes([1])
        data += (9_999).to_bytes(4, "little")
        data += bytes(64)
        account = account_dict(bytes(data), PUMP_FEE_SHARE_PROGRAM)  # space == len(data): no dataSlice was used
        with self.assertRaises(DecodeError) as caught:
            decode_sharing_config(CONFIG_ADDR, account)
        self.assertNotIsInstance(caught.exception, TruncatedConfig)

    def test_read_sharing_config_delegates_to_decode_sharing_config_exactly_once(self):
        import inspect

        source = inspect.getsource(read_sharing_config)
        self.assertEqual(source.count("decode_sharing_config"), 1)

    def test_read_sharing_config_still_reads_charlie_through_the_shared_decoder(self):
        class Curve:
            mint = MINT_A
            creator = CONFIG_ADDR

        class Rpc:
            def accounts(self, addresses):
                data = full_config_bytes(MINT_A, [(ADDR_3, 10_000)])
                return [account_dict(data, PUMP_FEE_SHARE_PROGRAM)]

        config = read_sharing_config(Rpc(), Curve())
        self.assertEqual(config.mint, MINT_A)
        self.assertEqual(config.shareholders, ((ADDR_3, 10_000),))


# -- legs.classify_split (D-26) -----------------------------------------------
class TestClassifySplit(unittest.TestCase):
    def _registry(self):
        return Registry(program_id=None, grandfathered_sol_burn=frozenset({ADDR_3}))

    def test_ops_only(self):
        split = split_of(
            type("Cfg", (), {"mint": MINT_A, "shareholders": ((ADDR_1, 10_000),)})(), self._registry()
        )
        self.assertEqual(classify_split(split), "ops-only")

    def test_sol_burn_only_via_grandfathered_address(self):
        split = split_of(
            type("Cfg", (), {"mint": MINT_A, "shareholders": ((ADDR_3, 10_000),)})(), self._registry()
        )
        self.assertEqual(classify_split(split), "sol_burn-only")

    def test_mixed_with_two_non_zero_legs(self):
        split = Split(sol_burn=5_000, burn=0, paid=5_000, attributions=())
        self.assertEqual(classify_split(split), "mixed")

    def test_none_for_no_shareholders(self):
        split = split_of(type("Cfg", (), {"mint": MINT_A, "shareholders": ()})(), self._registry())
        self.assertEqual(classify_split(split), "none")

    def test_accepts_a_plain_dict_the_shape_publish_classification_hands_it(self):
        self.assertEqual(classify_split({"sol_burn": 10_000, "burn": 0, "paid": 0}), "sol_burn-only")


# -- publish.classification ---------------------------------------------------
class TestPublishClassification(unittest.TestCase):
    def test_none_in_none_out(self):
        self.assertIsNone(publish.classification(None))

    def test_delegates_to_classify_split(self):
        self.assertEqual(publish.classification({"sol_burn": 0, "burn": 0, "paid": 10_000}), "ops-only")


# -- evidence.sharing_config ---------------------------------------------------
def make_config(address, mint, holders, admin_revoked=True, admin=ADMIN, version=2, status=1) -> SharingConfig:
    return SharingConfig(
        address=address,
        mint=mint,
        version=version,
        status=status,
        admin=admin,
        admin_revoked=admin_revoked,
        shareholders=tuple(holders),
    )


class TestSharingConfigTable(unittest.TestCase):
    def test_first_insert_returns_true_repeat_insert_returns_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = Evidence(Path(tmp) / "e.db")
            config = make_config(CONFIG_ADDR, MINT_A, [(ADDR_1, 10_000)])
            self.assertTrue(evidence.record_sharing_config(config))
            self.assertFalse(evidence.record_sharing_config(config))
            rows = evidence.connection.execute(
                "SELECT COUNT(*) AS n FROM sharing_config WHERE address = ?", (CONFIG_ADDR,)
            ).fetchone()
            self.assertEqual(rows["n"], 1)
            evidence.close()

    def test_repeat_insert_advances_last_seen_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = Evidence(Path(tmp) / "e.db")
            config = make_config(CONFIG_ADDR, MINT_A, [(ADDR_1, 10_000)])
            evidence.record_sharing_config(config, recorded_at=100)
            evidence.record_sharing_config(config, recorded_at=200)
            row = evidence.connection.execute(
                "SELECT first_seen, last_seen FROM sharing_config WHERE address = ?", (CONFIG_ADDR,)
            ).fetchone()
            self.assertEqual(row["first_seen"], 100)
            self.assertEqual(row["last_seen"], 200)
            evidence.close()

    def test_reconfiguration_inserts_new_row_and_supersedes_the_old_one_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = Evidence(Path(tmp) / "e.db")
            first = make_config(CONFIG_ADDR, MINT_A, [(ADDR_1, 10_000)])
            evidence.record_sharing_config(first, recorded_at=100)
            second = make_config(CONFIG_ADDR, MINT_A, [(ADDR_1, 5_000), (ADDR_2, 5_000)])
            evidence.record_sharing_config(second, recorded_at=200)

            rows = evidence.connection.execute(
                "SELECT * FROM sharing_config WHERE address = ? ORDER BY first_seen", (CONFIG_ADDR,)
            ).fetchall()
            self.assertEqual(len(rows), 2)
            self.assertIsNotNone(rows[0]["superseded_at"])
            self.assertIsNone(rows[1]["superseded_at"])
            # the superseded row's own recorded fields are unchanged
            self.assertEqual(rows[0]["shareholder_count"], 1)
            self.assertEqual(rows[1]["shareholder_count"], 2)

            current = evidence.current_sharing_configs()
            self.assertEqual(len(current), 1)
            self.assertEqual(current[0]["shareholder_count"], 2)
            evidence.close()

    def test_sharing_config_counts_computed_by_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = Evidence(Path(tmp) / "e.db")
            fixtures = [
                make_config("addr-1", "mint-1", [(ADDR_1, 10_000)], admin_revoked=True),
                make_config("addr-2", "mint-2", [(ADDR_1, 10_000)], admin_revoked=False),
                make_config("addr-3", "mint-3", [(ADDR_1, 5_000), (ADDR_2, 5_000)], admin_revoked=True),
                make_config("addr-4", "mint-4", [(ADDR_1, 5_000), (ADDR_2, 5_000)], admin_revoked=False),
                make_config("addr-5", "mint-5", [(ADDR_1, 5_000), (ADDR_2, 5_000)], admin_revoked=False),
                make_config("addr-6", "mint-6", [(ADDR_1, 10_000)], admin_revoked=True),
            ]
            for config in fixtures:
                evidence.record_sharing_config(config)

            counts = evidence.sharing_config_counts()
            self.assertEqual(counts["enumerated"], 6)
            self.assertEqual(counts["multi_shareholder"], 3)
            self.assertEqual(counts["admin_revoked"], 3)
            self.assertEqual(counts["prospects"], 2)  # multi-shareholder AND not revoked

            # Changing one config's admin_revoked changes the prospect count.
            evidence.connection.execute(
                "UPDATE sharing_config SET admin_revoked = 1 WHERE address = ?", ("addr-4",)
            )
            evidence.connection.commit()
            counts_after = evidence.sharing_config_counts()
            self.assertEqual(counts_after["prospects"], 1)
            evidence.close()

    def test_sharing_config_for_returns_the_current_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = Evidence(Path(tmp) / "e.db")
            evidence.record_sharing_config(make_config(CONFIG_ADDR, MINT_A, [(ADDR_1, 10_000)]))
            row = evidence.sharing_config_for(MINT_A)
            self.assertEqual(row["address"], CONFIG_ADDR)
            self.assertIsNone(evidence.sharing_config_for(MINT_B))
            evidence.close()


# -- coverage.config_observation -----------------------------------------------
class TestConfigObservation(unittest.TestCase):
    def test_checks_are_exactly_config_mint_and_split_sum(self):
        config = make_config(CONFIG_ADDR, MINT_A, [(ADDR_1, 10_000)])
        obs = coverage.config_observation(config)
        self.assertEqual([c.name for c in obs.checks], ["CONFIG_MINT", "SPLIT_SUM"])
        self.assertIsNotNone(obs.verdict)

    def test_split_sum_mismatch_withholds_split_naming_split_sum(self):
        config = make_config(CONFIG_ADDR, MINT_A, [(ADDR_1, 9_999)])
        obs = coverage.config_observation(config)
        publisher = publish.Publisher(obs)
        with self.assertRaises(publish.Withheld) as caught:
            publisher.figure(invariants.SPLIT)
        names = [name for name, _status, _detail in caught.exception.reasons]
        self.assertIn("SPLIT_SUM", names)
        self.assertIsNone(publish.classification(None))

    def test_valid_split_publishes_and_classification_matches(self):
        config = make_config(CONFIG_ADDR, MINT_A, [(ADDR_1, 10_000)])
        obs = coverage.config_observation(config)
        publisher = publish.Publisher(obs)
        value, backs = publisher.figure(invariants.SPLIT)
        self.assertEqual(value, {"sol_burn": 0, "burn": 0, "paid": 10_000})
        self.assertIn("CONFIG_MINT", backs)
        self.assertIn("SPLIT_SUM", backs)
        self.assertEqual(publish.classification(value), "ops-only")

    def test_config_observation_mint_comes_from_the_config_itself(self):
        config = make_config(CONFIG_ADDR, MINT_A, [(ADDR_1, 10_000)])
        obs = coverage.config_observation(config)
        self.assertEqual(obs.mint, MINT_A)


# -- coverage.sweep -------------------------------------------------------------
class TestSweep(unittest.TestCase):
    def test_sweep_partitions_every_entry_into_decoded_truncated_or_refused(self):
        decodable = program_account_entry(
            "addr-decodable",
            account_dict(full_config_bytes(MINT_A, [(ADDR_1, 10_000)]), PUMP_FEE_SHARE_PROGRAM),
        )
        truncated_full = full_config_bytes(MINT_B, [(ADDR_1, 3_000), (ADDR_2, 3_000), (ADDR_3, 4_000)])
        truncated = program_account_entry(
            "addr-truncated",
            account_dict(
                truncated_full[:SINGLE_SHAREHOLDER_SLICE],
                PUMP_FEE_SHARE_PROGRAM,
                space=SHARING_CONFIG_ACCOUNT_BYTES,
            ),
        )
        refused = program_account_entry(
            "addr-refused",
            account_dict(full_config_bytes(MINT_A, [(ADDR_1, 10_000)]), PUMP_PROGRAM),  # wrong owner
        )
        entries = [decodable, truncated, refused]
        rpc = FakeProgramAccountsRpc(entries)

        with tempfile.TemporaryDirectory() as tmp:
            evidence = Evidence(Path(tmp) / "e.db")
            progress = []
            result = coverage.sweep(rpc, evidence, on_progress=progress.append)

            self.assertEqual(result["returned"], 3)
            self.assertEqual(result["decoded"], 1)
            self.assertEqual(result["truncated"], 1)
            self.assertEqual(result["refused"], 1)
            self.assertEqual(result["decoded"] + result["truncated"] + result["refused"], result["returned"])
            self.assertEqual(result["truncated_addresses"], ["addr-truncated"])
            self.assertTrue(progress)  # on_progress was called; sweep itself never prints

            recorded = evidence.sharing_config_for(MINT_A)
            self.assertIsNotNone(recorded)
            self.assertIsNone(evidence.sharing_config_for(MINT_B))  # the truncated one was never recorded
            evidence.close()

    def test_sweep_requests_the_single_shareholder_slice_and_discriminator_filter(self):
        rpc = FakeProgramAccountsRpc([])
        with tempfile.TemporaryDirectory() as tmp:
            evidence = Evidence(Path(tmp) / "e.db")
            coverage.sweep(rpc, evidence)
            evidence.close()
        call = rpc.calls[0]
        self.assertEqual(call["program_id"], PUMP_FEE_SHARE_PROGRAM)
        self.assertEqual(call["data_slice"], {"offset": 0, "length": SINGLE_SHAREHOLDER_SLICE})
        self.assertIn(coverage.CONFIG_FILTER, call["filters"])

    def test_sweep_never_prints(self):
        import ast
        import inspect

        source = inspect.getsource(coverage.sweep)
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotEqual(node.func.id, "print")

    def test_narrowing_filters_add_shareholder_count_and_revoked_memcmps(self):
        rpc = FakeProgramAccountsRpc([])
        with tempfile.TemporaryDirectory() as tmp:
            evidence = Evidence(Path(tmp) / "e.db")
            coverage.sweep(rpc, evidence, holders=8, revoked=False)
            evidence.close()
        filters = rpc.calls[0]["filters"]
        self.assertEqual(len(filters), 3)
        self.assertIn(coverage.CONFIG_FILTER, filters)


if __name__ == "__main__":
    unittest.main()

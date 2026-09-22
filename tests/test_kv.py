"""`indexer/kv.py`: the Upstash REST wire format, and `MemoryKV`, the fake the
claim and bot tests use. No network."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes_http import FakeOpener  # noqa: E402
from indexer.kv import KV, KVError, MemoryKV  # noqa: E402

URL = "https://example-kv.invalid"


class TestRest(unittest.TestCase):
    def test_command_wire_format(self):
        opener = FakeOpener({URL: {"result": "OK"}})
        kv = KV(URL + "/", "TOKEN", opener=opener)
        self.assertEqual(kv.command("SET", "k", "v", "EX", 60), "OK")
        request = opener.last()
        self.assertEqual(request.full_url, URL)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer TOKEN")
        self.assertEqual(json.loads(request.data), ["SET", "k", "v", "EX", "60"])

    def test_json_helpers(self):
        opener = FakeOpener({URL: {"result": json.dumps({"a": 1})}})
        kv = KV(URL, "T", opener=opener)
        self.assertEqual(kv.get_json("k"), {"a": 1})
        kv.set_json("k", {"a": 1}, ex=5)
        self.assertEqual(json.loads(opener.last().data), ["SET", "k", '{"a":1}', "EX", "5"])
        kv.push("q", {"x": 1})
        self.assertEqual(json.loads(opener.last().data), ["RPUSH", "q", '{"x":1}'])
        self.assertEqual(kv.pop("q"), {"a": 1})
        self.assertEqual(json.loads(opener.last().data)[0], "LPOP")

    def test_missing_is_none(self):
        kv = KV(URL, "T", opener=FakeOpener({URL: {"result": None}}))
        self.assertIsNone(kv.get_json("k"))
        self.assertIsNone(kv.pop("q"))

    def test_errors(self):
        with self.assertRaises(KVError):
            KV(URL, "T", opener=FakeOpener({URL: {"error": "WRONGTYPE"}})).command("GET", "k")
        with self.assertRaises(KVError):
            KV(URL, "T", opener=FakeOpener({URL: 401})).command("GET", "k")
        with self.assertRaises(KVError):
            KV(URL, "T", opener=FakeOpener({URL: b"not json"})).command("GET", "k")
        with self.assertRaises(KVError):
            KV("", "", opener=FakeOpener({})).command("GET", "k")


class TestMemory(unittest.TestCase):
    def test_values_and_expiry(self):
        clock = [1000.0]
        kv = MemoryKV(now=lambda: clock[0])
        kv.set_json("k", {"v": 1}, ex=10)
        self.assertEqual(kv.get_json("k"), {"v": 1})
        clock[0] = 1010.0
        self.assertIsNone(kv.get_json("k"))
        kv.set_json("p", [1, 2])
        clock[0] = 99999.0
        self.assertEqual(kv.get_json("p"), [1, 2])
        kv.delete("p")
        self.assertIsNone(kv.get_json("p"))

    def test_queue_is_fifo(self):
        kv = MemoryKV()
        for i in range(3):
            kv.push("q", {"i": i})
        self.assertEqual(kv.command("LLEN", "q"), 3)
        self.assertEqual([kv.pop("q")["i"] for _ in range(3)], [0, 1, 2])
        self.assertIsNone(kv.pop("q"))

    def test_unknown_command(self):
        with self.assertRaises(KVError):
            MemoryKV().command("FLUSHALL")


if __name__ == "__main__":
    unittest.main()

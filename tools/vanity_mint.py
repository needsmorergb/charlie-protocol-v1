"""Grind ed25519 mint keypairs whose address ends in a chosen base58 suffix, on
the GPU.

    py -3.13 tools/vanity_mint.py --test                 # GPU agrees with indexer/ed25519?
    py -3.13 tools/vanity_mint.py 1nc1n --count 20       # grind twenty
    py -3.13 tools/vanity_mint.py 1nc1n --ignore-case    # 1NC1N, 1Nc1n ... also count
    py -3.13 tools/vanity_mint.py --env                  # the pool as CHARLIE_MINT_POOL

Every hit the GPU reports is re-derived with the repo's own Python ed25519 and
base58 before it is kept, so a kernel bug can waste time but cannot write a
wrong key. Output is a JSON list of {"address", "keypair"} where keypair is
the 64-byte Solana format (seed + public key), appended to on each run. The
file name matches the *keypair*.json gitignore family: these are live private
keys and never belong in a commit.

Kernel launches are kept under about half a second each, because Windows
resets a display driver that a single launch holds for two.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import pyopencl as cl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from indexer import base58, ed25519  # noqa: E402
from indexer.curve import _D, _Q  # noqa: E402

KERNEL = Path(__file__).with_name("vanity_mint.cl")
DEFAULT_OUT = ROOT / "mint-pool-keypairs.json"
MAX_FOUND = 256


# -- SHA-512 constants, derived rather than remembered --------------------------


def _primes(n: int) -> list[int]:
    out, k = [], 2
    while len(out) < n:
        if all(k % p for p in out):
            out.append(k)
        k += 1
    return out


def _iroot(x: int, n: int) -> int:
    lo, hi = 0, 1 << ((x.bit_length() + n - 1) // n + 1)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if mid**n <= x:
            lo = mid
        else:
            hi = mid - 1
    return lo


def sha512_constants() -> tuple[list[int], list[int]]:
    primes = _primes(80)
    k = [_iroot(p << 192, 3) & ((1 << 64) - 1) for p in primes]
    h0 = [_iroot(p << 128, 2) & ((1 << 64) - 1) for p in primes[:8]]
    assert hashlib.sha512(b"").digest()[:8] == b"\xcf\x83\xe15\x7e\xef\xb8\xbd"  # sanity
    return k, h0


# -- the field-element limb form the kernel uses --------------------------------

_BITS = [26, 25] * 5


def to_limbs(v: int) -> list[int]:
    v %= _Q
    limbs = []
    for bits in _BITS:
        limbs.append(v & ((1 << bits) - 1))
        v >>= bits
    assert v == 0
    return limbs


def build_table() -> np.ndarray:
    """table[i][k] = k * 2^(8i) * B, Niels form (y+x, y-x, 2dxy), limbs."""
    inv = lambda z: pow(z, _Q - 2, _Q)
    two_d = 2 * _D % _Q
    table = np.zeros((32, 256, 30), dtype=np.int32)
    window = ed25519._B
    for i in range(32):
        point = ed25519._IDENTITY
        for k in range(256):
            x, y, z, _ = point
            zi = inv(z)
            x, y = x * zi % _Q, y * zi % _Q
            table[i, k, 0:10] = to_limbs(y + x)
            table[i, k, 10:20] = to_limbs(y - x)
            table[i, k, 20:30] = to_limbs(two_d * x * y)
            point = ed25519._add(point, window)
        for _ in range(8):
            window = ed25519._add(window, window)
    return table


# -- base58 tail as a residue -----------------------------------------------------


def suffix_targets(suffix: str, ignore_case: bool) -> tuple[list[int], int]:
    """The address ends in `suffix` iff int(key, big-endian) mod 58^len is one
    of these values."""
    import itertools

    for ch in suffix:
        if ch not in base58.ALPHABET:
            raise SystemExit(f"{ch!r} is not a base58 character (no 0, O, I or l)")
    variants = [suffix]
    if ignore_case:
        pools = [{c.lower(), c.upper()} & set(base58.ALPHABET) for c in suffix]
        variants = ["".join(p) for p in itertools.product(*pools)]
    modulus = 58 ** len(suffix)
    targets = []
    for v in variants:
        n = 0
        for ch in v:
            n = n * 58 + base58.ALPHABET.index(ch)
        targets.append(n)
    return sorted(set(targets)), modulus


# -- the device -------------------------------------------------------------------


class Grinder:
    def __init__(self, device_index: int | None):
        devices = [d for p in cl.get_platforms() for d in p.get_devices(cl.device_type.GPU)]
        if not devices:
            raise SystemExit("no OpenCL GPU found")
        if device_index is None:
            device_index = max(range(len(devices)), key=lambda i: devices[i].max_compute_units)
        self.device = devices[device_index]
        self.ctx = cl.Context([self.device])
        self.queue = cl.CommandQueue(self.ctx)
        k, h0 = sha512_constants()
        src = KERNEL.read_text()
        src = src.replace("SHA512_K_LITERALS", ", ".join(f"0x{v:016x}UL" for v in k))
        src = src.replace("SHA512_H0_LITERALS", ", ".join(f"0x{v:016x}UL" for v in h0))
        self.prog = cl.Program(self.ctx, src).build(options=os.environ.get("VANITY_CL_OPTS", "").split())
        mf = cl.mem_flags
        self.table = cl.Buffer(self.ctx, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=build_table())
        self.prefix_buf = cl.Buffer(self.ctx, mf.READ_ONLY, 24)

    def seeds(self, prefix: bytes, batch: int, n: int) -> list[bytes]:
        return [prefix + batch.to_bytes(4, "little") + g.to_bytes(4, "little") for g in range(n)]

    def pubkeys(self, prefix: bytes, batch: int, n: int) -> list[bytes]:
        cl.enqueue_copy(self.queue, self.prefix_buf, prefix)
        out = cl.Buffer(self.ctx, cl.mem_flags.WRITE_ONLY, 32 * n)
        self.prog.pubkeys(self.queue, (n,), None, self.table, self.prefix_buf, np.uint32(batch), out)
        host = np.empty(32 * n, dtype=np.uint8)
        cl.enqueue_copy(self.queue, host, out).wait()
        raw = host.tobytes()
        return [raw[i * 32:(i + 1) * 32] for i in range(n)]

    def grind_batch(self, prefix: bytes, batch: int, n: int, targets, modulus: int) -> list[bytes]:
        mf = cl.mem_flags
        cl.enqueue_copy(self.queue, self.prefix_buf, prefix)
        tbuf = cl.Buffer(self.ctx, mf.READ_ONLY | mf.COPY_HOST_PTR,
                         hostbuf=np.array(targets, dtype=np.uint32))
        count = cl.Buffer(self.ctx, mf.READ_WRITE | mf.COPY_HOST_PTR, hostbuf=np.zeros(1, dtype=np.uint32))
        found = cl.Buffer(self.ctx, mf.WRITE_ONLY, 32 * MAX_FOUND)
        self.prog.grind(self.queue, (n,), None, self.table, self.prefix_buf, np.uint32(batch),
                        tbuf, np.uint32(len(targets)), np.uint32(modulus),
                        count, found, np.uint32(MAX_FOUND))
        c = np.zeros(1, dtype=np.uint32)
        cl.enqueue_copy(self.queue, c, count).wait()
        hits = min(int(c[0]), MAX_FOUND)
        if not hits:
            return []
        host = np.empty(32 * MAX_FOUND, dtype=np.uint8)
        cl.enqueue_copy(self.queue, host, found).wait()
        raw = host.tobytes()
        return [raw[i * 32:(i + 1) * 32] for i in range(hits)]


# -- host-side truth ---------------------------------------------------------------


def address_of(seed: bytes) -> str:
    return base58.encode(ed25519.public_key(seed))


def self_test(g: Grinder, n: int = 2048) -> None:
    prefix = os.urandom(24)
    got = g.pubkeys(prefix, 7, n)
    seeds = g.seeds(prefix, 7, n)
    bad = 0
    for seed, pk in zip(seeds, got):
        if pk != ed25519.public_key(seed):
            bad += 1
    if bad:
        raise SystemExit(f"self-test FAILED: {bad}/{n} public keys differ from indexer/ed25519")
    print(f"self-test passed: {n}/{n} GPU public keys match indexer/ed25519 on {g.device.name}")


def load_pool(path: Path) -> list[dict]:
    if path.exists():
        return json.loads(path.read_text())
    return []


def save_pool(path: Path, pool: list[dict]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(pool, indent=1))
    tmp.replace(path)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("suffix", nargs="?", default="1nc1n")
    ap.add_argument("--count", type=int, default=20, help="how many to have in the pool when done")
    ap.add_argument("--ignore-case", action="store_true")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--device", type=int, default=None, help="OpenCL GPU index (default: most compute units)")
    ap.add_argument("--test", action="store_true", help="only check the kernel against the Python code")
    ap.add_argument("--env", action="store_true",
                    help="print the pool as the CHARLIE_MINT_POOL value the launch door reads, and exit")
    ap.add_argument("--target-seconds", type=float, default=0.4, help="aim for launches about this long")
    args = ap.parse_args()

    if "keypair" not in args.out.name:
        raise SystemExit("the output name must contain 'keypair' so the gitignore rule covers it")

    if args.env:
        # Seeds only, base58, comma-separated: what indexer/mint_pool.py parses.
        # This is the private half of every key in the pool; paste it into the
        # deployment's secret store and nowhere else.
        pool = load_pool(args.out)
        print(",".join(base58.encode(bytes(p["keypair"][:32])) for p in pool))
        return

    t0 = time.time()
    g = Grinder(args.device)
    print(f"device: {g.device.name}, table built in {time.time() - t0:.1f}s")
    self_test(g)
    if args.test:
        return

    targets, modulus = suffix_targets(args.suffix, args.ignore_case)
    expected = modulus / len(targets)
    print(f"suffix {args.suffix!r}{' (any case)' if args.ignore_case else ''}: "
          f"one hit per {expected:,.0f} keys on average")

    pool = load_pool(args.out)
    have = [p for p in pool if p["address"].endswith(args.suffix) or
            (args.ignore_case and p["address"].lower().endswith(args.suffix.lower()))]
    print(f"pool {args.out.name}: {len(pool)} keys, {len(have)} with this suffix, want {args.count}")
    known = {p["address"] for p in pool}

    n = 1 << 16
    batch = 0
    tried = 0
    start = time.time()
    last_report = start
    while len(have) < args.count:
        prefix = os.urandom(24)
        t = time.time()
        hits = g.grind_batch(prefix, batch, n, targets, modulus)
        dt = time.time() - t
        tried += n
        batch += 1
        for seed in hits:
            address = address_of(seed)  # the Python code is the judge
            ok = address.endswith(args.suffix) or (args.ignore_case and address.lower().endswith(args.suffix.lower()))
            if not ok:
                print(f"  GPU false positive {address} (ignored)")
                continue
            if address in known:
                continue
            entry = {"address": address, "keypair": list(seed + ed25519.public_key(seed))}
            pool.append(entry)
            have.append(entry)
            known.add(address)
            save_pool(args.out, pool)
            print(f"  {len(have):>3}/{args.count}  {address}   after {tried:,} keys, {time.time() - start:.0f}s")
        # keep each launch short (driver watchdog) but big enough to fill the GPU
        if dt < args.target_seconds / 2 and n < (1 << 24):
            n *= 2
        elif dt > args.target_seconds * 2 and n > (1 << 12):
            n //= 2
        if time.time() - last_report > 15:
            rate = tried / (time.time() - start)
            eta = (args.count - len(have)) * expected / rate
            print(f"  {rate / 1e6:.2f} M keys/s, {tried:,} tried, batch {n:,}, eta ~{eta / 60:.0f} min")
            last_report = time.time()

    print(f"done: {len(have)} keys ending in {args.suffix!r} in {args.out}, {tried:,} keys tried, "
          f"{(time.time() - start) / 60:.1f} min")


if __name__ == "__main__":
    main()

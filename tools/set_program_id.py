"""Point every surface at a program id, and recompute the PDAs that follow.

Closing a Solana program permanently retires its id, so every redeploy of
the devnet proof is a NEW id, and the id appears in five places that must
not drift: `declare_id!`, the driver, the cross-check test's expected PDAs,
the program README, and the flywheel page's recorded run.

Doing that by hand got it wrong once. This does it in one pass.

    python -m tools.set_program_id GFA3nG9geMhpPXaExVLGYBtj6aJX7S125dLzv4EcXGiG
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# A base58 pubkey, 32-44 chars. Anchored to the places an id is written so a
# stray base58-looking string elsewhere is not rewritten.
PUBKEY = r"[1-9A-HJ-NP-Za-km-z]{32,44}"


def current_id() -> str:
    text = (ROOT / "program" / "src" / "lib.rs").read_text(encoding="utf-8")
    match = re.search(rf'declare_id!\("({PUBKEY})"\)', text)
    if not match:
        raise SystemExit("no declare_id! found in program/src/lib.rs")
    return match.group(1)


def pdas(program_id: str) -> dict:
    sys.path.insert(0, str(ROOT))
    from indexer.base58 import pubkey_bytes
    from indexer.curve import find_program_address

    mint = "So11111111111111111111111111111111111111112"
    seeds = {
        "route": [b"route", pubkey_bytes(mint)],
        "collector": [b"collect", pubkey_bytes(mint)],
        "burn_pool": [b"burn", pubkey_bytes(mint)],
        "charlie_pool": [b"charlie"],
    }
    return {
        name: find_program_address(parts, program_id)[0]
        for name, parts in seeds.items()
    }


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        raise SystemExit(__doc__)
    new = argv[0]
    if not re.fullmatch(PUBKEY, new):
        raise SystemExit(f"{new} is not a base58 pubkey")

    old = current_id()
    if old == new:
        print(f"already {new}")
        return 0

    for relative in (
        "program/src/lib.rs",
        "tools/flywheel_devnet.py",
        "program/README.md",
        "program/tests/derive.rs",
    ):
        path = ROOT / relative
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        if old in text:
            path.write_text(text.replace(old, new), encoding="utf-8")
            print(f"  {relative}: {old} -> {new}")

    # The cross-check test pins the PDAs, which all move with the id.
    derive = ROOT / "program" / "tests" / "derive.rs"
    if derive.exists():
        text = derive.read_text(encoding="utf-8")
        for function, name in (
            ("route_pda", "route"),
            ("collector_pda", "collector"),
            ("burn_pool_pda", "burn_pool"),
            ("charlie_pool_pda", "charlie_pool"),
        ):
            address = pdas(new)[name]
            text = re.sub(
                rf'({function}\((?:&mint)?\)\.0\.to_string\(\),\s*\n\s*")({PUBKEY})(")',
                lambda m, a=address: m.group(1) + a + m.group(3),
                text,
            )
        derive.write_text(text, encoding="utf-8")
        print("  program/tests/derive.rs: PDAs recomputed")

    for name, address in pdas(new).items():
        print(f"    {name:14s} {address}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

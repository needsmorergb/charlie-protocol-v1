"""The two distinct buy-and-burn routes Charlie operates.

`buyback` is for a launched coin: its operator spends that launch's own
budget to buy and destroy that launch's mint.  `charlie-buyback` is for the
protocol: it spends only the collection wallet's balance to buy and destroy
`$CHARLIE`.  Keeping the route decision here prevents a CLI spelling mistake
from silently turning one programme into the other.
"""

from __future__ import annotations

from . import legs

CHARLIE_MINT = "8FhAXv2tfXUpyMbJsHDHX9zfiEb9PERzFWSY9sgLpump"


def launch(mint: str, wallet: str) -> None:
    """Refuse the protocol mint on the launch-token route."""
    if mint == CHARLIE_MINT:
        raise ValueError("$CHARLIE has its own route: use `indexer charlie-buyback`")


def charlie(wallet: str) -> None:
    """Require the collection wallet for the protocol's $CHARLIE route."""
    destination = legs.TOLL_DESTINATION
    if destination is None:
        raise ValueError("$CHARLIE buyback is unavailable: no collection wallet is configured")
    if wallet != destination:
        raise ValueError(
            "$CHARLIE buyback must use the protocol collection wallet "
            f"{destination}, not {wallet}"
        )

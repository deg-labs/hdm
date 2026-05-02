import threading
import logging
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from hyperliquid.info import Info
from hyperliquid.utils import constants

from .models import Trade


logger = logging.getLogger("hdm.ws")


class HyperliquidUserFillsMonitor:
    def __init__(
        self,
        addresses: List[str],
        callback: Optional[Callable[[Trade], None]] = None,
        base_url: str = constants.MAINNET_API_URL,
    ):
        self.addresses = [a.lower() for a in addresses]
        self.address_set = set(self.addresses)
        self.callback = callback
        self.info = Info(base_url)
        self._stop_event = threading.Event()

    @staticmethod
    def build_trade_from_fill(fill: Dict[str, Any], address: str) -> Trade:
        timestamp = datetime.fromtimestamp(int(fill.get("time", 0)) / 1000)
        size = float(fill.get("sz", 0))
        start_position = float(fill.get("startPosition", 0))
        liquidation = fill.get("liquidation") or None
        is_liquidation = isinstance(liquidation, dict)
        liquidation_kind = None
        if is_liquidation:
            liquidation_kind = "Full Liq." if abs(size) >= abs(start_position) else "Partial Liq."
        return Trade(
            timestamp=timestamp,
            address=address,
            coin=fill.get("coin", "Unknown"),
            side="SELL" if fill.get("side") == "A" else "BUY",
            size=size,
            price=float(fill.get("px", 0)),
            trade_type="FILL",
            direction=fill.get("dir"),
            tx_hash=fill.get("hash"),
            fee=float(fill.get("fee", 0)),
            fee_token=fill.get("feeToken"),
            start_position=start_position,
            closed_pnl=float(fill.get("closedPnl", 0)),
            is_liquidation=is_liquidation,
            liquidation_kind=liquidation_kind,
        )

    def _on_user_fills(self, msg: Dict[str, Any]) -> None:
        if self._stop_event.is_set():
            return
        if not isinstance(msg, dict):
            return

        data = msg.get("data") or {}
        address = (data.get("user") or "").lower()
        if address not in self.address_set:
            return

        fills = data.get("fills") or []
        for fill in fills:
            if not isinstance(fill, dict):
                logger.debug("ignoring non-dict fill address=%s fill_type=%s", address, type(fill).__name__)
                continue
            try:
                trade = self.build_trade_from_fill(fill, address)
            except Exception as e:
                logger.warning("failed to parse fill address=%s error=%s fill=%s", address, e, fill)
                continue
            if self.callback:
                self.callback(trade)

    def start(self) -> None:
        if not self.addresses:
            raise ValueError("No addresses configured to monitor")

        for address in self.addresses:
            self.info.subscribe({"type": "userFills", "user": address}, self._on_user_fills)

        while not self._stop_event.wait(1):
            pass

    def stop(self) -> None:
        self._stop_event.set()
        try:
            if self.info and self.info.ws_manager:
                self.info.ws_manager.stop()
        except Exception as e:
            logger.debug("error stopping websocket manager error=%s", e)
            pass

from __future__ import annotations

from app.exchanges.base import ExchangeClient
from app.exchanges.demo import DemoClient
from app.exchanges.gate import GateClient
from app.exchanges.mexc import MexcClient

NAMES = {"mexc": "MEXC", "gate": "Gate"}


def create_client(exchange_id: str, demo: bool, scan_rate: float, proxy: str = "") -> ExchangeClient:
    if demo:
        return DemoClient(exchange_id, NAMES[exchange_id], scan_rate=scan_rate, proxy=proxy)
    cls = {"mexc": MexcClient, "gate": GateClient}[exchange_id]
    return cls(scan_rate=scan_rate, proxy=proxy)

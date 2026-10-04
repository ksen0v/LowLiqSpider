from __future__ import annotations

from app.detectors.base import Detector
from app.detectors.ping_pong import PingPongDetector
from app.detectors.price_change import PriceChangeDetector
from app.detectors.spread import SpreadDetector
from app.detectors.volume_spike import VolumeSpikeDetector
from app.detectors.wicks import WicksDetector

DETECTORS: list[Detector] = [
    PingPongDetector(),
    SpreadDetector(),
    WicksDetector(),
    VolumeSpikeDetector(),
    PriceChangeDetector(),
]

DETECTORS_BY_KEY = {d.key: d for d in DETECTORS}

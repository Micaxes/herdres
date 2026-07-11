"""Put bridge/ on sys.path so tests import the connector as `herdres_connector.*`
(mirrors how bridge/herdres.py runs it), independent of pytest's rootdir."""
from __future__ import annotations

import sys
from pathlib import Path

_BRIDGE = Path(__file__).resolve().parent.parent
if str(_BRIDGE) not in sys.path:
    sys.path.insert(0, str(_BRIDGE))

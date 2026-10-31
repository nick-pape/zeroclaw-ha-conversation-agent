"""Load client.py on its own: the package __init__ imports Home Assistant."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_PATH = Path(__file__).parent.parent / "custom_components" / "zeroclaw_conversation" / "client.py"
_spec = importlib.util.spec_from_file_location("zeroclaw_client", _PATH)
client = importlib.util.module_from_spec(_spec)
sys.modules["zeroclaw_client"] = client
_spec.loader.exec_module(client)

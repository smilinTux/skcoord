#!/usr/bin/env python3
"""Compatibility loader for the packaged controlled evidence vocabulary."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from skcoord.evidence_vocab import *  # noqa: F401,F403,E402

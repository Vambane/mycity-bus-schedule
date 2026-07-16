"""
conftest.py — Shared pytest configuration
==========================================
Makes the repo root importable (the project is a flat script layout, not a
package), mirroring the sys.path pattern used by run_etl.py.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

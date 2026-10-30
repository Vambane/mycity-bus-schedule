"""Halte: MyCiTi and UCT Jammie timetables (FastAPI application)."""
import sys
from pathlib import Path

__version__ = "2.0.0"

# The timetable logic (journey.py, disruption.py, ls_ui.py, journey_map.py,
# system_map.py) lives at the repo root as plain modules, not a package.
# Make the root importable once, here, so every route can import those
# modules at the top of the file whatever directory the server starts in.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

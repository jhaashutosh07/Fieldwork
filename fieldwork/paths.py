"""Project root: the checkout by default, or FIELDWORK_HOME when the package is installed elsewhere (Docker)."""
import os
from pathlib import Path

ROOT = Path(os.environ.get("FIELDWORK_HOME") or Path(__file__).resolve().parents[1])

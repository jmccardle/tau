"""Put this experiment's modules on ``sys.path``; the directory name is not importable."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

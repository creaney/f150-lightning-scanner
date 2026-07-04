"""
Pytest configuration -- adds the repo root to sys.path so tests can import scanner.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

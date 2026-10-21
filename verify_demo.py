"""verify_demo.py — обратная совместимость: python verify_demo.py store

Логика переехала в scada/demo.py (там же объяснение, что и почему ловится).
"""
from __future__ import annotations

import sys
from pathlib import Path

from scada.demo import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

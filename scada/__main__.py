"""python -m scada — то же, что python -m scada.run."""
from __future__ import annotations

import sys

from .run import main

if __name__ == "__main__":
    sys.exit(main())

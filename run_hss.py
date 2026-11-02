"""PyInstaller entry script (kept outside the package so relative imports resolve)."""
import sys

from hss.__main__ import main

if __name__ == "__main__":
    sys.exit(main())

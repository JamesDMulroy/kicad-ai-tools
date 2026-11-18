"""KiCad IPC action: Adaptive Drag (selected footprints)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import panel  # noqa: E402

if __name__ == "__main__":
    sys.exit(panel.main("drag"))

"""Entry point: `python app/server.py` serves; `python app/server.py create-admin <username>` adds an admin."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bms  # noqa: E402 - needs the path set above

if __name__ == '__main__':
    bms.main(sys.argv[1:])

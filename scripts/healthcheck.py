#!/usr/bin/env python3
"""Docker healthcheck: is the bot's event loop still alive?

The bot rewrites a heartbeat file from a repeating job on the loop (see
`app._heartbeat_job`). A `pgrep python` check cannot tell a healthy idle bot
from one whose loop has wedged mid-`getUpdates`; a stale heartbeat can. Exit 0
if the marker is fresh, 1 otherwise — which is all Docker reads.

No dependencies beyond the standard library, so it runs in the same slim image
as the bot with no extra install.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

#: Allow a few missed beats (HEARTBEAT_INTERVAL is 60 s) before failing, so a
#: single slow tick under load does not flap the container unhealthy.
MAX_AGE_SECONDS = 180.0


def main() -> int:
    data_dir = Path(os.environ.get("DATA_DIR", "data"))
    heartbeat = data_dir / "health" / "heartbeat"
    try:
        age = time.time() - heartbeat.stat().st_mtime
    except FileNotFoundError:
        print("heartbeat file missing", file=sys.stderr)
        return 1
    if age > MAX_AGE_SECONDS:
        print(
            f"heartbeat is {age:.0f}s old (> {MAX_AGE_SECONDS:.0f}s)",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

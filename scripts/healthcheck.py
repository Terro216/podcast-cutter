#!/usr/bin/env python3
"""Docker healthcheck: are the event loop and Telegram polling both healthy?

The scheduled heartbeat alone cannot detect a stuck polling task while the
loop still runs. The polling marker is renewed only by successful getUpdates
requests, including empty responses. Both markers must be fresh.

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
    for name in ("heartbeat", "polling"):
        marker = data_dir / "health" / name
        try:
            age = time.time() - marker.stat().st_mtime
        except OSError:
            print(f"{name} heartbeat missing or unreadable", file=sys.stderr)
            return 1
        if age > MAX_AGE_SECONDS:
            print(
                f"{name} heartbeat is {age:.0f}s old (> {MAX_AGE_SECONDS:.0f}s)",
                file=sys.stderr,
            )
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

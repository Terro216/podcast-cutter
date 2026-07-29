"""Entry point. All the wiring lives in :mod:`podcast_cutter.app`."""

import sys

from podcast_cutter.app import run
from podcast_cutter.errors import PodcastCutterError

if __name__ == "__main__":
    try:
        run()
    except PodcastCutterError as exc:
        # Misconfiguration and missing ffmpeg are operator errors: report them
        # plainly instead of dumping a traceback.
        sys.exit(f"Startup failed: {exc}")
    except KeyboardInterrupt:
        pass

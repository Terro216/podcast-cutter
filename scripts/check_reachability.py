#!/usr/bin/env python3
"""How much of the podcast directory can this server actually fetch?

Some hosts — Spotify-owned Anchor most notably — reject requests from
datacenter IP addresses outright. No amount of retrying or reshaping the
request helps, so the only useful question is *how much* of the directory is
affected from where the bot runs.

Run it inside the container, so it measures the bot's egress rather than
yours:

    docker compose exec -T podcast-cutter python - < scripts/check_reachability.py

Or locally, which measures your own connection instead:

    python scripts/check_reachability.py [sample-size]
"""

from __future__ import annotations

import asyncio
import collections
import os
import sys
import urllib.parse
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from podcast_cutter.api import PodcastIndexClient  # noqa: E402
from podcast_cutter.audio import _BROWSERISH_USER_AGENT as UA  # noqa: E402
from podcast_cutter.config import load_settings  # noqa: E402
from podcast_cutter.errors import PodcastCutterError  # noqa: E402

#: Requests in flight at once. Some CDNs drop parallel connections from one
#: address, which shows up as a connect timeout and looks like a block when it
#: is really just the measurement being rude — the bot fetches one at a time.
#: Override with CONCURRENCY=1 to check whether a failure is real.
CONCURRENCY = int(os.getenv("CONCURRENCY") or 8)


async def _reach(
    client: httpx.AsyncClient, url: str, limiter: asyncio.Semaphore
) -> tuple[str, object]:
    """Return ``(host, status)`` for one enclosure, never raising.

    The host reported is the one that actually answered or failed, not the one
    in the enclosure URL. Podcast audio is almost always fronted by redirecting
    analytics prefixes, so blaming the first hop would point at the wrong
    service — it is usually innocent.
    """
    async with limiter:
        try:
            # A ranged GET rather than HEAD: plenty of CDNs reject HEAD but
            # serve ranges happily, which is what the bot itself relies on.
            async with client.stream(
                "GET",
                url,
                headers={"User-Agent": UA, "Accept": "*/*", "Range": "bytes=0-1"},
            ) as response:
                return urllib.parse.urlparse(str(response.url)).netloc, (
                    response.status_code
                )
        except httpx.HTTPError as exc:
            request = getattr(exc, "request", None)
            failed_at = str(request.url) if request is not None else url
            return urllib.parse.urlparse(failed_at).netloc, type(exc).__name__


async def main(sample: int) -> int:
    settings = load_settings()
    api = PodcastIndexClient(settings)

    print(f"Collecting up to {sample} episodes from trending feeds…")
    episodes = []
    try:
        for feed in await api.trending_feeds(sample):
            if len(episodes) >= sample:
                break
            try:
                episodes.extend((await api.list_episodes(feed.id))[:1])
            except PodcastCutterError:
                continue
    except PodcastCutterError as exc:
        print(f"Could not reach the directory: {exc}", file=sys.stderr)
        return 1
    finally:
        await api.aclose()

    if not episodes:
        print("No episodes to check.", file=sys.stderr)
        return 1

    limiter = asyncio.Semaphore(CONCURRENCY)
    async with httpx.AsyncClient(follow_redirects=True, timeout=25.0) as client:
        results = await asyncio.gather(
            *(_reach(client, ep.enclosure_url, limiter) for ep in episodes)
        )

    # 200 and 206 both mean "we got audio"; the bot asks for a range.
    good = [(h, s) for h, s in results if s in (200, 206)]
    bad = [(h, s) for h, s in results if s not in (200, 206)]

    print(f"\nChecked {len(results)} episodes")
    print(f"  reachable:   {len(good)}  ({len(good) / len(results) * 100:.0f}%)")
    print(f"  unreachable: {len(bad)}  ({len(bad) / len(results) * 100:.0f}%)")

    if bad:
        print("\nWhat failed, by host:")
        for (host, status), count in collections.Counter(bad).most_common(15):
            note = "  ← blocks this server" if status in (401, 403) else ""
            print(f"  {str(status):>14}  {host}{note}  ×{count}")

    blocked = sum(1 for _, s in bad if s in (401, 403))
    if blocked:
        share = blocked / len(results) * 100
        print(
            f"\n{blocked} of {len(results)} ({share:.0f}%) refuse this server's "
            "IP address outright. Only a different egress route changes that."
        )

    return 0


if __name__ == "__main__":
    size = int(sys.argv[1]) if len(sys.argv) > 1 else 25
    raise SystemExit(asyncio.run(main(size)))

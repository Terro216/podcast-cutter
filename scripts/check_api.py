"""Live smoke check against the Podcast Index API.

Not a unit test — it needs real credentials and network access. Replaces the
old ``test_byperson.py``, which sat in the repo root where pytest kept trying
to collect it as a test module.

    python scripts/check_api.py "Lex Fridman"
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from podcast_cutter.api import PodcastIndexClient  # noqa: E402
from podcast_cutter.config import load_settings  # noqa: E402
from podcast_cutter.errors import PodcastCutterError  # noqa: E402


async def main(query: str) -> int:
    settings = load_settings()
    client = PodcastIndexClient(settings)

    try:
        print(f"— search_feeds({query!r})")
        feeds, has_next = await client.search_feeds(query)
        for feed in feeds:
            print(f"   {feed.id:>10}  {feed.title} — {feed.author}")
        print(f"   has_next={has_next}")

        print(f"\n— search_episodes_by_person({query!r})")
        episodes = await client.search_episodes_by_person(query)
        for episode in episodes[:3]:
            print(f"   {episode.feed_title} / {episode.title}")
            print(f"      {episode.enclosure_url}")

        print("\n— trending_feeds()")
        for feed in (await client.trending_feeds(3)):
            print(f"   {feed.title}")

        print("\n— random_episode()")
        episode = await client.random_episode()
        print(f"   {episode.feed_title} / {episode.title}")
    except PodcastCutterError as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        await client.aclose()

    print("\nAll endpoints OK.")
    return 0


if __name__ == "__main__":
    term = sys.argv[1] if len(sys.argv) > 1 else "Lex Fridman"
    raise SystemExit(asyncio.run(main(term)))

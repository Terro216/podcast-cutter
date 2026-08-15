import pytest

from podcast_cutter.api import Episode, Feed, PodcastIndexClient
from podcast_cutter.config import Settings
from podcast_cutter.errors import NotFoundError


@pytest.fixture
def settings():
    return Settings(
        bot_token="t", api_key="k", api_secret="s", podcasts_per_page=5
    )


class TestCaching:
    """Identical directory queries within the TTL cost one network call."""

    def _client(self, settings, payload):
        client = PodcastIndexClient(settings)
        calls: list[str] = []

        async def fake_fetch(path, params):
            calls.append(path)
            return payload

        client._fetch = fake_fetch
        return client, calls

    async def test_identical_searches_hit_the_network_once(self, settings):
        client, calls = self._client(
            settings, {"feeds": [{"id": 1, "title": "Show", "author": "A"}]}
        )
        first, _ = await client.search_feeds("radiolab")
        second, _ = await client.search_feeds("radiolab")
        assert first == second
        assert len(calls) == 1

    async def test_different_queries_are_not_conflated(self, settings):
        client, calls = self._client(
            settings, {"feeds": [{"id": 1, "title": "Show", "author": "A"}]}
        )
        await client.search_feeds("radiolab")
        await client.search_feeds("serial")
        assert len(calls) == 2

    async def test_random_is_never_cached(self, settings):
        # Its whole point is a different answer each time.
        client, calls = self._client(
            settings,
            {
                "episodes": [
                    {
                        "id": 9,
                        "title": "Ep",
                        "feedTitle": "Show",
                        "enclosureUrl": "https://cdn.example.com/9.mp3",
                    }
                ]
            },
        )
        await client.random_episode()
        await client.random_episode()
        assert len(calls) == 2

    async def test_an_expired_entry_is_refetched(self, settings):
        client, calls = self._client(
            settings, {"feeds": [{"id": 1, "title": "Show", "author": "A"}]}
        )
        await client.search_feeds("radiolab")
        # Age every entry past its TTL rather than sleeping through one.
        client._cache = {
            key: (0.0, payload) for key, (_, payload) in client._cache.items()
        }
        await client.search_feeds("radiolab")
        assert len(calls) == 2


class TestFeedParsing:
    def test_parses_a_normal_feed(self):
        parsed = Feed.from_api({"id": 42, "title": "Show", "author": "Me"})
        assert parsed == Feed(id="42", title="Show", author="Me")

    def test_ids_are_normalised_to_strings(self):
        # The API returns ints; callback data and lookups are strings. Mixing
        # the two is what forced str() comparisons all over the old code.
        assert Feed.from_api({"id": 42}).id == "42"

    def test_supplies_placeholders_for_missing_text(self):
        parsed = Feed.from_api({"id": 1})
        assert parsed.title and parsed.author

    def test_flattens_multiline_titles(self):
        parsed = Feed.from_api({"id": 1, "title": "Line\nTwo"})
        assert parsed.title == "Line Two"

    def test_drops_entries_without_an_id(self):
        assert Feed.from_api({"title": "Show"}) is None


class TestEpisodeParsing:
    def test_parses_a_normal_episode(self):
        parsed = Episode.from_api(
            {
                "id": 7,
                "title": "Ep 7",
                "feedTitle": "Show",
                "enclosureUrl": "https://cdn.example.com/7.mp3",
                "duration": 3600,
            }
        )
        assert parsed.id == "7"
        assert parsed.enclosure_url == "https://cdn.example.com/7.mp3"
        assert parsed.duration == 3600

    @pytest.mark.parametrize(
        "raw",
        [
            {"id": 1},  # no enclosure at all
            {"id": 1, "enclosureUrl": ""},
            {"id": 1, "enclosureUrl": "   "},
            {"id": 1, "enclosureUrl": "ftp://example.com/a.mp3"},
            {"enclosureUrl": "https://example.com/a.mp3"},  # no id
        ],
    )
    def test_drops_unplayable_entries(self, raw):
        # Offering an episode we cannot fetch means the user only finds out
        # after picking it and typing an interval.
        assert Episode.from_api(raw) is None

    def test_drops_video_enclosures(self):
        assert (
            Episode.from_api(
                {
                    "id": 1,
                    "enclosureUrl": "https://example.com/a.mp4",
                    "enclosureType": "video/mp4",
                }
            )
            is None
        )

    @pytest.mark.parametrize("value", [0, -1, None, "3600", 1.5])
    def test_unusable_durations_become_none(self, value):
        parsed = Episode.from_api(
            {"id": 1, "enclosureUrl": "https://e.com/a.mp3", "duration": value}
        )
        assert parsed.duration is None


class TestSearchPagination:
    """The endpoint has no offset parameter, so paging is done locally."""

    @staticmethod
    def _client_returning(settings, feeds):
        client = PodcastIndexClient(settings)

        async def fake_get(path, params):
            # Emulate `max` capping the result set.
            return {"feeds": feeds[: params["max"]]}

        client._get = fake_get  # type: ignore[method-assign]
        return client

    def _feeds(self, count):
        return [{"id": i, "title": f"Feed {i}", "author": "A"} for i in range(count)]

    @pytest.mark.asyncio
    async def test_page_one_starts_at_the_first_result(self, settings):
        client = self._client_returning(settings, self._feeds(20))
        window, has_next = await client.search_feeds("q", page=1)

        # Regression: the old implementation sliced [-per_page:] off an
        # over-fetched list and so always skipped the top hit.
        assert [f.title for f in window] == [f"Feed {i}" for i in range(5)]
        assert has_next is True

    @pytest.mark.asyncio
    async def test_later_pages_do_not_overlap(self, settings):
        client = self._client_returning(settings, self._feeds(20))
        page1, _ = await client.search_feeds("q", page=1)
        page2, _ = await client.search_feeds("q", page=2)

        assert {f.id for f in page1}.isdisjoint({f.id for f in page2})
        assert [f.id for f in page2] == ["5", "6", "7", "8", "9"]

    @pytest.mark.asyncio
    async def test_last_page_reports_no_next(self, settings):
        client = self._client_returning(settings, self._feeds(10))
        window, has_next = await client.search_feeds("q", page=2)
        assert len(window) == 5
        assert has_next is False

    @pytest.mark.asyncio
    async def test_partial_last_page(self, settings):
        client = self._client_returning(settings, self._feeds(7))
        window, has_next = await client.search_feeds("q", page=2)
        assert len(window) == 2
        assert has_next is False

    @pytest.mark.asyncio
    async def test_no_results_raises_not_found(self, settings):
        client = self._client_returning(settings, [])
        with pytest.raises(NotFoundError):
            await client.search_feeds("nothing")

    @pytest.mark.asyncio
    async def test_paging_past_the_end_raises_not_found(self, settings):
        client = self._client_returning(settings, self._feeds(3))
        with pytest.raises(NotFoundError):
            await client.search_feeds("q", page=4)

    @pytest.mark.asyncio
    async def test_unparseable_entries_are_skipped_not_fatal(self, settings):
        client = self._client_returning(
            settings, [{"title": "no id"}, {"id": 1, "title": "good"}]
        )
        window, _ = await client.search_feeds("q")
        assert [f.title for f in window] == ["good"]

import re

import pytest

from podcast_cutter import keyboards as kb


class TestCallbackVocabulary:
    def test_round_trips(self):
        assert kb.parse_callback("ep:1234") == ("ep", "1234")
        assert kb.parse_callback("feed:99") == ("feed", "99")

    def test_navigation_is_namespaced_away_from_ids(self):
        # The old code compared raw callback data against "next_page", so any
        # id equal to that string would have been read as a page turn.
        prefix, _ = kb.parse_callback(kb.NAV_NEXT)
        assert prefix == kb.NAV_PREFIX
        assert prefix not in (kb.FEED_PREFIX, kb.EPISODE_PREFIX)

    @pytest.mark.parametrize("data", [None, "", "garbage", "no-colon-here"])
    def test_unknown_payloads_are_reported_not_raised(self, data):
        assert kb.parse_callback(data) == ("", "")

    def test_ids_containing_a_colon_keep_their_tail(self):
        assert kb.parse_callback("ep:a:b") == ("ep", "a:b")


class TestMenuRegex:
    @pytest.mark.parametrize("label", kb.MENU_BUTTONS)
    def test_matches_the_button_exactly(self, label):
        assert re.match(kb.menu_regex(label), label)

    def test_does_not_match_a_substring(self):
        pattern = kb.menu_regex(kb.BTN_TRENDING)
        assert not re.match(pattern, f"{kb.BTN_TRENDING} please")

    def test_escapes_regex_metacharacters(self):
        # Button labels are literals, not patterns.
        assert re.match(kb.menu_regex("a.b"), "a.b")
        assert not re.match(kb.menu_regex("a.b"), "axb")

    def test_combines_several_labels(self):
        pattern = kb.menu_regex(*kb.MENU_BUTTONS)
        assert all(re.match(pattern, label) for label in kb.MENU_BUTTONS)
        assert not re.match(pattern, "something else")


class Item:
    def __init__(self, item_id, title):
        self.id = item_id
        self.title = title


def _payloads(markup):
    return [
        button.callback_data for row in markup.inline_keyboard for button in row
    ]


class TestChoiceKeyboard:
    def _build(self, items, **kwargs):
        return kb.choice_keyboard(
            items,
            kb.EPISODE_PREFIX,
            id_of=lambda i: i.id,
            label_of=lambda i: i.title,
            **kwargs,
        )

    def test_one_button_per_item_plus_cancel(self):
        markup = self._build([Item("1", "A"), Item("2", "B")])
        assert _payloads(markup) == ["ep:1", "ep:2", kb.NAV_CANCEL]

    def test_adds_navigation_only_where_it_leads_somewhere(self):
        assert kb.NAV_NEXT not in _payloads(self._build([Item("1", "A")]))

        both = _payloads(self._build([Item("1", "A")], has_prev=True, has_next=True))
        assert kb.NAV_PREV in both and kb.NAV_NEXT in both

    def test_always_offers_a_way_out(self):
        # Every list is escapable without typing /cancel.
        assert kb.NAV_CANCEL in _payloads(self._build([]))

    def test_skips_items_whose_payload_exceeds_telegram_limit(self):
        # Truncating callback data would silently select the wrong episode.
        markup = self._build([Item("9" * 200, "huge"), Item("2", "fine")])
        assert _payloads(markup) == ["ep:2", kb.NAV_CANCEL]

    def test_labels_are_single_line_and_bounded(self):
        markup = self._build([Item("1", "Very\nlong\ntitle " + "x" * 300)])
        label = markup.inline_keyboard[0][0].text
        assert "\n" not in label
        assert len(label) <= 60

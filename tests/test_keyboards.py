import re

import pytest

from podcast_cutter import keyboards as kb


def payloads(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def labels(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


def styles(markup):
    return {b.text: b.style for row in markup.inline_keyboard for b in row}


class Item:
    def __init__(self, item_id, title):
        self.id = item_id
        self.title = title


def build(items, **kwargs):
    return kb.choice_keyboard(
        items,
        kb.EPISODE_PREFIX,
        id_of=lambda i: i.id,
        label_of=lambda i: i.title,
        **kwargs,
    )


class TestCallbackVocabulary:
    def test_round_trips(self):
        assert kb.parse_callback("ep:1234") == ("ep", "1234")

    def test_navigation_is_namespaced_away_from_ids(self):
        prefix, _ = kb.parse_callback(kb.NAV_BACK)
        assert prefix not in (kb.FEED_PREFIX, kb.EPISODE_PREFIX)

    @pytest.mark.parametrize("data", [None, "", "garbage", "no-colon"])
    def test_unknown_payloads_are_reported_not_raised(self, data):
        assert kb.parse_callback(data) == ("", "")

    def test_ids_containing_a_colon_keep_their_tail(self):
        assert kb.parse_callback("ep:a:b") == ("ep", "a:b")

    def test_every_payload_fits_telegrams_limit(self):
        # Telegram rejects callback_data above 64 bytes outright.
        fixed = [
            kb.NAV_BACK, kb.NAV_MENU, kb.NAV_CANCEL, kb.NAV_NOOP,
            kb.ACTION_CUT, kb.ACTION_RETRY, kb.ACTION_TOGGLE_VOICE,
            kb.ACTION_NEW_CLIP, kb.ACTION_CLEAR_FILTER,
        ]
        assert all(len(value.encode()) <= 64 for value in fixed)


class TestMenuRegex:
    @pytest.mark.parametrize("label", kb.MENU_BUTTONS)
    def test_matches_exactly(self, label):
        assert re.match(kb.menu_regex(label), label)

    def test_does_not_match_a_substring(self):
        assert not re.match(kb.menu_regex(kb.BTN_TRENDING), f"{kb.BTN_TRENDING} x")

    def test_escapes_metacharacters(self):
        assert re.match(kb.menu_regex("a.b"), "a.b")
        assert not re.match(kb.menu_regex("a.b"), "axb")


class TestPagination:
    def test_hidden_for_a_single_page(self):
        assert kb.pagination_row(1, 1) == []

    def test_shows_position_and_total(self):
        row = kb.pagination_row(3, 7)
        assert [b.text for b in row] == ["‹", "3/7", "›"]

    def test_no_previous_on_the_first_page(self):
        assert [b.text for b in kb.pagination_row(1, 4)] == ["1/4", "›"]

    def test_no_next_on_the_last_page(self):
        assert [b.text for b in kb.pagination_row(4, 4)] == ["‹", "4/4"]

    def test_the_counter_does_nothing_when_tapped(self):
        counter = next(b for b in kb.pagination_row(2, 5) if b.text == "2/5")
        assert counter.callback_data == kb.NAV_NOOP

    def test_arrows_target_the_neighbouring_pages(self):
        row = kb.pagination_row(3, 7)
        assert row[0].callback_data == f"{kb.PAGE_PREFIX}:2"
        assert row[2].callback_data == f"{kb.PAGE_PREFIX}:4"


class TestChoiceKeyboard:
    def test_one_button_per_item_plus_a_way_out(self):
        markup = build([Item("1", "A"), Item("2", "B")])
        assert payloads(markup)[:2] == ["ep:1", "ep:2"]
        assert kb.NAV_BACK in payloads(markup)

    def test_every_list_offers_back_and_menu(self):
        # No screen may be a dead end.
        markup = build([])
        assert kb.NAV_BACK in payloads(markup)
        assert kb.NAV_MENU in payloads(markup)

    def test_skips_items_whose_payload_is_too_long(self):
        markup = build([Item("9" * 200, "huge"), Item("2", "fine")])
        assert "ep:2" in payloads(markup)
        assert not any(len(p.encode()) > 64 for p in payloads(markup))

    def test_labels_are_single_line_and_bounded(self):
        markup = build([Item("1", "Very\nlong " + "x" * 300)])
        label = markup.inline_keyboard[0][0].text
        assert "\n" not in label and len(label) <= 60

    def test_extra_rows_are_appended(self):
        markup = build([Item("1", "A")], extra_rows=[[kb.clear_filter_button()]])
        assert kb.ACTION_CLEAR_FILTER in payloads(markup)


class TestIntervalKeyboard:
    def test_offers_presets_moves_and_a_cut(self):
        markup = kb.interval_keyboard(60, max_length=900, as_voice=False)
        data = payloads(markup)

        assert f"{kb.LENGTH_PREFIX}:30" in data
        assert f"{kb.MOVE_PREFIX}:-15" in data
        assert f"{kb.MOVE_PREFIX}:60" in data
        assert kb.ACTION_CUT in data

    def test_the_cut_button_is_the_primary_action(self):
        markup = kb.interval_keyboard(60, max_length=900, as_voice=False)
        cut = next(
            b
            for row in markup.inline_keyboard
            for b in row
            if b.callback_data == kb.ACTION_CUT
        )
        assert cut.style == kb.STYLE_PRIMARY

    def test_the_current_length_is_marked(self):
        markup = kb.interval_keyboard(60, max_length=900, as_voice=False)
        selected = [
            b
            for row in markup.inline_keyboard
            for b in row
            if b.callback_data == f"{kb.LENGTH_PREFIX}:60"
        ]
        assert selected and selected[0].style == kb.STYLE_SUCCESS

    def test_presets_longer_than_the_limit_are_hidden(self):
        # Offering a button that always errors is worse than not offering it.
        markup = kb.interval_keyboard(30, max_length=60, as_voice=False)
        data = payloads(markup)
        assert f"{kb.LENGTH_PREFIX}:30" in data
        assert f"{kb.LENGTH_PREFIX}:300" not in data

    @pytest.mark.parametrize("as_voice", [True, False])
    def test_the_format_toggle_shows_the_current_choice(self, as_voice):
        markup = kb.interval_keyboard(60, max_length=900, as_voice=as_voice)
        toggle = next(
            b
            for row in markup.inline_keyboard
            for b in row
            if b.callback_data == kb.ACTION_TOGGLE_VOICE
        )
        assert ("voice" in toggle.text) is as_voice

    def test_always_offers_a_way_back(self):
        markup = kb.interval_keyboard(60, max_length=900, as_voice=False)
        assert kb.NAV_BACK in payloads(markup)


class TestResultKeyboard:
    def test_offers_nudges_and_a_repeat(self):
        markup = kb.result_keyboard("some episode")
        data = payloads(markup)
        assert f"{kb.SHIFT_PREFIX}:-15" in data
        assert f"{kb.SHIFT_PREFIX}:15" in data
        assert kb.ACTION_NEW_CLIP in data

    def test_sharing_opens_the_chat_picker(self):
        markup = kb.result_keyboard("some episode")
        share = next(
            b
            for row in markup.inline_keyboard
            for b in row
            if b.switch_inline_query is not None
        )
        assert share.switch_inline_query == "some episode"


class TestErrorKeyboard:
    def test_retry_is_one_tap_and_prominent(self):
        markup = kb.error_keyboard()
        retry = markup.inline_keyboard[0][0]
        assert retry.callback_data == kb.ACTION_RETRY
        assert retry.style == kb.STYLE_PRIMARY

    def test_still_offers_an_exit(self):
        assert kb.NAV_MENU in payloads(kb.error_keyboard())


class TestStyles:
    def test_only_values_telegram_accepts_are_used(self):
        allowed = {None, "primary", "success", "danger"}
        markups = [
            kb.interval_keyboard(60, max_length=900, as_voice=False),
            kb.result_keyboard("x"),
            kb.error_keyboard(),
            kb.menu_keyboard(has_recent=True),
            kb.cancel_keyboard(),
            build([Item("1", "A")]),
        ]
        for markup in markups:
            assert set(styles(markup).values()) <= allowed

    def test_cancel_is_marked_destructive(self):
        cancel = kb.cancel_keyboard().inline_keyboard[0][0]
        assert cancel.style == kb.STYLE_DANGER


class TestMainMenu:
    def test_is_persistent_so_it_never_disappears(self):
        markup = kb.main_menu()
        assert markup.is_persistent and markup.resize_keyboard

    def test_lists_every_menu_button(self):
        shown = {b.text for row in markup_rows(kb.main_menu()) for b in row}
        assert shown == set(kb.MENU_BUTTONS)


class TestBotProfile:
    """The texts published at startup, against Telegram's own limits.

    Overrunning either is rejected with a BadRequest that startup only logs, so
    the profile would quietly stay whatever it was.
    """

    def test_the_short_description_fits(self):
        from podcast_cutter.app import SHORT_DESCRIPTION

        assert 0 < len(SHORT_DESCRIPTION) <= 120

    def test_the_description_fits_once_the_username_is_filled_in(self):
        from podcast_cutter.app import DESCRIPTION

        filled = DESCRIPTION.format(username="podcast_cutter_bot")
        assert 0 < len(filled) <= 512
        assert "{username}" not in filled

    def test_the_description_carries_no_markup(self):
        from podcast_cutter.app import DESCRIPTION, SHORT_DESCRIPTION

        # Telegram renders neither HTML nor Markdown here; tags would show up
        # as literal angle brackets on the empty-chat screen.
        for text in (DESCRIPTION, SHORT_DESCRIPTION):
            assert "<" not in text and "</" not in text


def markup_rows(markup):
    return markup.keyboard

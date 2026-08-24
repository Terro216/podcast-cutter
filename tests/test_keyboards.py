import pytest

from podcast_cutter import i18n
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
            kb.ACTION_NEW_CLIP, kb.ACTION_CLEAR_FILTER, kb.ACTION_SUBTITLES,
        ]
        assert all(len(value.encode()) <= 64 for value in fixed)


class TestMenuActions:
    @pytest.mark.parametrize("lang", i18n.LANGUAGES)
    def test_every_label_routes_in_every_language(self, lang):
        # A reply-keyboard press arrives as its label. Labels on a client's
        # screen outlive a language switch, so every language's labels must
        # keep routing, not just the current one's.
        for key, action in kb._MENU_LABEL_ACTIONS:
            assert kb.menu_action(i18n.t(lang, key)) == action

    def test_free_text_is_not_a_menu_press(self):
        assert kb.menu_action("radiolab") is None

    def test_labels_do_not_collide_across_languages(self):
        # Every (language, label) pair must stay distinct, or one language's
        # button would silently trigger another's action.
        labels = [
            i18n.t(lang, key)
            for lang in i18n.LANGUAGES
            for key, _ in kb._MENU_LABEL_ACTIONS
        ]
        assert len(labels) == len(set(labels))


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
        markup = kb.interval_keyboard(60, max_length=900)
        data = payloads(markup)

        assert f"{kb.LENGTH_PREFIX}:30" in data
        assert f"{kb.MOVE_PREFIX}:-15" in data
        assert f"{kb.MOVE_PREFIX}:60" in data
        assert kb.ACTION_CUT in data

    def test_the_cut_button_is_the_primary_action(self):
        markup = kb.interval_keyboard(60, max_length=900)
        cut = next(
            b
            for row in markup.inline_keyboard
            for b in row
            if b.callback_data == kb.ACTION_CUT
        )
        assert cut.style == kb.STYLE_PRIMARY

    def test_the_current_length_is_marked(self):
        markup = kb.interval_keyboard(60, max_length=900)
        selected = [
            b
            for row in markup.inline_keyboard
            for b in row
            if b.callback_data == f"{kb.LENGTH_PREFIX}:60"
        ]
        assert selected and selected[0].style == kb.STYLE_SUCCESS

    def test_presets_longer_than_the_limit_are_hidden(self):
        # Offering a button that always errors is worse than not offering it.
        markup = kb.interval_keyboard(30, max_length=60)
        data = payloads(markup)
        assert f"{kb.LENGTH_PREFIX}:30" in data
        assert f"{kb.LENGTH_PREFIX}:300" not in data

    def test_offers_all_four_delivery_formats(self):
        markup = kb.interval_keyboard(60, max_length=900)
        data = payloads(markup)
        for fmt in ("audio", "voice", "note", "video"):
            assert f"{kb.FORMAT_PREFIX}:{fmt}" in data

    @pytest.mark.parametrize("send_as", ["audio", "voice", "note", "video"])
    def test_the_active_format_is_marked(self, send_as):
        markup = kb.interval_keyboard(60, max_length=900, send_as=send_as)
        active = next(
            b
            for row in markup.inline_keyboard
            for b in row
            if b.callback_data == f"{kb.FORMAT_PREFIX}:{send_as}"
        )
        assert active.text.startswith("●")
        assert active.style == kb.STYLE_SUCCESS

    def test_skins_appear_only_when_a_video_format_is_chosen(self):
        # A permanent block of decoration would bury the cut button under
        # choices that mean nothing for audio.
        audio = payloads(kb.interval_keyboard(60, max_length=900))
        assert not any(p.startswith(f"{kb.SKIN_PREFIX}:") for p in audio)
        for fmt in ("note", "video"):
            offered = payloads(
                kb.interval_keyboard(60, max_length=900, send_as=fmt)
            )
            for skin in kb.SKIN_LABELS:
                assert f"{kb.SKIN_PREFIX}:{skin}" in offered

    def test_artwork_only_skins_disappear_when_the_episode_has_no_image(self):
        offered = payloads(
            kb.interval_keyboard(
                60, max_length=900, send_as="note", has_artwork=False
            )
        )
        assert f"{kb.SKIN_PREFIX}:cover" not in offered
        assert f"{kb.SKIN_PREFIX}:vinyl" not in offered
        assert f"{kb.SKIN_PREFIX}:dvd" in offered
        assert f"{kb.SKIN_PREFIX}:matrix" in offered

    def test_loop_skins_disappear_when_no_loop_is_available(self):
        offered = payloads(
            kb.interval_keyboard(
                60,
                max_length=900,
                send_as="video",
                available_skins=("aurora", "party", "lava", "matrix"),
            )
        )
        for skin in ("roblox", "gta", "asmr", "subway"):
            assert f"{kb.SKIN_PREFIX}:{skin}" not in offered
        assert f"{kb.SKIN_PREFIX}:aurora" in offered

    def test_every_skin_sits_in_some_row(self):
        # The rows are hand-split for width; a skin added to the labels but
        # not to a row would silently never be offered.
        assert {key for row in kb._SKIN_ROWS for key in row} == set(
            kb.SKIN_LABELS
        )

    def test_the_active_skin_is_marked(self):
        markup = kb.interval_keyboard(
            60, max_length=900, send_as="note", skin="matrix"
        )
        active = next(
            b
            for row in markup.inline_keyboard
            for b in row
            if b.callback_data == f"{kb.SKIN_PREFIX}:matrix"
        )
        assert active.text.startswith("●")

    def test_always_offers_a_way_back(self):
        markup = kb.interval_keyboard(60, max_length=900)
        assert kb.NAV_BACK in payloads(markup)

    def test_subtitles_are_an_explicit_bottom_toggle_for_video(self):
        markup = kb.interval_keyboard(
            60,
            max_length=900,
            send_as="note",
            can_subtitle=True,
            subtitles=False,
            transcript_ready=False,
        )
        assert markup.inline_keyboard[-2][0].callback_data == kb.ACTION_SUBTITLES
        assert "few minutes" in markup.inline_keyboard[-2][0].text

    def test_ready_and_enabled_subtitles_have_honest_labels(self):
        ready = kb.interval_keyboard(
            60,
            max_length=900,
            send_as="video",
            can_subtitle=True,
            transcript_ready=True,
        )
        assert any("instant" in label for label in labels(ready))
        enabled = kb.interval_keyboard(
            60,
            max_length=900,
            send_as="video",
            can_subtitle=True,
            subtitles=True,
        )
        button = next(
            b
            for row in enabled.inline_keyboard
            for b in row
            if b.callback_data == kb.ACTION_SUBTITLES
        )
        assert button.style == kb.STYLE_SUCCESS


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


class TestReskinOnResult:
    def test_skins_are_offered_after_a_visual_format(self):
        for fmt in ("note", "video"):
            markup = kb.result_keyboard("x", send_as=fmt, skin="cover")
            data = payloads(markup)
            for skin in kb.SKIN_LABELS:
                assert f"{kb.RESKIN_PREFIX}:{skin}" in data

    def test_no_skins_after_plain_audio(self):
        # Skin buttons that each spend a cut have no business under an mp3.
        for fmt in (None, "audio", "voice"):
            markup = kb.result_keyboard("x", send_as=fmt)
            data = [p for p in payloads(markup) if p]
            assert not any(p.startswith(f"{kb.RESKIN_PREFIX}:") for p in data)

    def test_artwork_only_reskins_disappear_without_an_image(self):
        data = payloads(
            kb.result_keyboard(
                "x", send_as="video", skin="aurora", has_artwork=False
            )
        )
        assert f"{kb.RESKIN_PREFIX}:cover" not in data
        assert f"{kb.RESKIN_PREFIX}:vinyl" not in data
        assert f"{kb.RESKIN_PREFIX}:dvd" in data

    def test_unavailable_loop_reskins_disappear(self):
        data = payloads(
            kb.result_keyboard(
                "x",
                send_as="video",
                available_skins=("cover", "aurora", "dvd"),
            )
        )
        for skin in ("roblox", "gta", "asmr", "subway"):
            assert f"{kb.RESKIN_PREFIX}:{skin}" not in data

    def test_the_sent_skin_is_marked(self):
        markup = kb.result_keyboard("x", send_as="note", skin="matrix")
        active = next(
            b
            for row in markup.inline_keyboard
            for b in row
            if b.callback_data == f"{kb.RESKIN_PREFIX}:matrix"
        )
        assert active.text.startswith("●")
        assert active.style == kb.STYLE_SUCCESS


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
            kb.interval_keyboard(60, max_length=900),
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

    @pytest.mark.parametrize("lang", i18n.LANGUAGES)
    def test_lists_every_menu_button(self, lang):
        shown = {b.text for row in markup_rows(kb.main_menu(lang)) for b in row}
        assert shown == {
            i18n.t(lang, key) for key, _ in kb._MENU_LABEL_ACTIONS
        }

    @pytest.mark.parametrize("lang", i18n.LANGUAGES)
    def test_reply_menu_includes_language(self, lang):
        assert i18n.t(lang, "btn_language") in {
            b.text for row in markup_rows(kb.main_menu(lang)) for b in row
        }


class TestLanguageKeyboard:
    def test_offers_every_language_by_its_own_name(self):
        markup = kb.language_keyboard("en")
        texts = labels(markup)
        for lang in i18n.LANGUAGES:
            assert any(i18n.LANGUAGE_NAMES[lang] in text for text in texts)
            assert f"{kb.LANG_PREFIX}:{lang}" in payloads(markup)

    def test_the_current_language_is_marked(self):
        markup = kb.language_keyboard("ru")
        active = next(
            b
            for row in markup.inline_keyboard
            for b in row
            if b.callback_data == f"{kb.LANG_PREFIX}:ru"
        )
        assert active.text.startswith("●")
        assert active.style == kb.STYLE_SUCCESS

    def test_still_offers_a_way_out(self):
        assert kb.NAV_BACK in payloads(kb.language_keyboard("en"))


class TestBotProfile:
    """The texts published at startup, against Telegram's own limits.

    Overrunning either is rejected with a BadRequest that startup only logs, so
    the profile would quietly stay whatever it was. Every language published
    is bound by the same limits, so every language is checked.
    """

    @pytest.mark.parametrize("lang", i18n.LANGUAGES)
    def test_the_short_description_fits(self, lang):
        assert 0 < len(i18n.t(lang, "short_description")) <= 120

    @pytest.mark.parametrize("lang", i18n.LANGUAGES)
    def test_the_description_fits_once_the_username_is_filled_in(self, lang):
        filled = i18n.t(lang, "description", username="podcast_cutter_bot")
        assert 0 < len(filled) <= 512
        assert "{username}" not in filled

    @pytest.mark.parametrize("lang", i18n.LANGUAGES)
    def test_the_description_carries_no_markup(self, lang):
        # Telegram renders neither HTML nor Markdown here; tags would show up
        # as literal angle brackets on the empty-chat screen.
        for key in ("description", "short_description"):
            text = i18n.t(lang, key) if key == "short_description" else i18n.t(
                lang, key, username="x"
            )
            assert "<" not in text and "</" not in text

    @pytest.mark.parametrize("lang", i18n.LANGUAGES)
    def test_command_descriptions_fit_telegrams_limits(self, lang):
        # BotCommand descriptions are capped at 256 characters.
        for name, description in i18n.bot_commands(lang):
            assert name.isascii() and name.islower()
            assert 0 < len(description) <= 256


def markup_rows(markup):
    return markup.keyboard

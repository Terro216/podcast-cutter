"""Every sentence a user can read, in every language the bot speaks.

One flat table per language, addressed by key. The rules that keep it honest:

* English is the reference: a key exists if and only if it is in ``_EN``, and
  a test holds the other tables to exactly the same key set, so a missing
  translation is a failing test rather than a silent English sentence in the
  middle of a Russian screen.
* Strings carry their placeholders (``{username}``) and their HTML markup with
  them. Translators see the whole sentence; code never glues fragments into
  grammar it cannot know.
* Pluralised nouns live in ``_PLURALS`` as full form sets — two for English,
  three for Russian — because "1 эпизода" is exactly the bug hard-coded
  ``+'s'`` produces in reverse.

The user's language is resolved in :func:`resolve_language`: an explicit
choice (stored in the database) wins, then Telegram's ``language_code``, then
English. Auto-detection is deliberately *not* stored, so a user who switches
their Telegram client switches the bot with it — until the day they choose.
"""

from __future__ import annotations

DEFAULT_LANGUAGE = "en"
#: Languages the bot speaks. Order is the order language buttons appear in.
LANGUAGES = ("en", "ru")

#: What the language chooser calls each language — never translated, because
#: a person who cannot read the current language must still find their own.
LANGUAGE_NAMES = {"en": "English", "ru": "Русский"}


def resolve_language(stored: str | None, telegram_code: str | None) -> str:
    """The language to answer in: chosen > Telegram's client > English."""
    if stored in LANGUAGES:
        return stored
    if telegram_code:
        prefix = telegram_code.split("-")[0].lower()
        if prefix in LANGUAGES:
            return prefix
    return DEFAULT_LANGUAGE


# ---------------------------------------------------------------------------
# English — the reference table
# ---------------------------------------------------------------------------

_EN = {
    # -- first contact and commands ------------------------------------
    "welcome": (
        "👋 <b>Welcome!</b>\n\n"
        "I cut a short piece out of a podcast episode and send it back, so you "
        "can share the good bit instead of a two-hour link.\n\n"
        "Once you've picked an episode, tell me when it starts: "
        "<code>12:30</code> for a clip from there, or <code>12:30-14:00</code> "
        "for an exact range. The ◀ ▶ buttons nudge it until it's right.\n\n"
        "In any other chat, type <code>@{username}</code> and a name to hand "
        "someone an episode without leaving the conversation.\n\n"
        "Send me a podcast name to start."
    ),
    "help": (
        "🎙 <b>Podcast Cutter</b>\n\n"
        "Find an episode, tell me a moment, get just that part back.\n\n"
        "<b>Finding something</b>\n"
        "/search — a podcast by name\n"
        "/person — find episodes: a guest, a topic, a title\n"
        "/trending — what's popular\n"
        "/surprise — a random episode\n"
        "/recent — episodes you looked at\n"
        "/language — language / язык\n"
        "/terms · /privacy — rules and stored data\n"
        "/mydata · /delete_me — inspect or erase your data\n"
        "/copyright — rights and takedown requests\n"
        "/cancel — back to the main menu\n"
        "/help — this message\n\n"
        "<b>Picking the moment</b>\n"
        "Send <code>12:30</code> for a clip starting there, or "
        "<code>12:30-14:00</code> for an exact range.\n"
        "Then nudge it with the ◀ ▶ buttons until it's right.\n\n"
        "<b>Anywhere else</b>\n"
        "Type <code>@{username}</code> in any chat to share an episode "
        "without leaving the conversation.\n\n"
        "<i>Independent tool · directory data via Podcast Index.</i>"
    ),
    "unknown_command": "I don't know that command — /help lists the ones I do.",
    "opened_from_link": "🔗 Opened from a shared link.",
    "no_journal": "No journal configured.",
    "started_fresh": (
        "⏱ It had been a while, so I started fresh — "
        "if this was meant for an earlier screen, "
        "just navigate there again."
    ),
    "stale_menu": "⌛ That menu is out of date — here's a fresh start.",
    "generic_error": "Something went wrong on my side. Please try again.",
    "terms_prompt": (
        "⚖️ <b>Before using Podcast Cutter</b>\n\n"
        "You choose the material and instruct the bot to process it. By "
        "continuing, you confirm that you have permission or another lawful "
        "basis to make and share each clip, and accept responsibility for "
        "your use. Piracy and infringement are prohibited.\n\n"
        "<a href=\"{terms_url}\">Terms of Use</a> · "
        "<a href=\"{privacy_url}\">Privacy Policy</a>\n"
        "Version: {version}"
    ),
    "terms_accepted": "✅ Terms accepted. You can use the bot now.",
    "terms_declined": (
        "No problem — the bot will not search, download or process content "
        "without acceptance. /terms and /privacy remain available."
    ),
    "terms_text": (
        "⚖️ <b>Terms of Use</b>\n\n"
        "You may process only material you are authorised to use or whose use "
        "is otherwise lawful. You choose the source, interval and destination "
        "and are responsible for the resulting clip and its distribution. "
        "Illegal, infringing, deceptive and commercial use without the "
        "necessary rights is prohibited. Access may be suspended and content "
        "may be blocked or deleted. The service is independent, provided as "
        "is and is not endorsed by Podcast Index or podcast publishers.\n\n"
        "Full terms: <a href=\"{url}\">{url}</a>"
    ),
    "privacy_text": (
        "🔐 <b>Privacy</b>\n\n"
        "Stored: Telegram user id, chosen language, Terms acceptance, recent "
        "episodes and limited operational events. A chat id and episode URL "
        "are kept while a transcription job is pending; terminal job rows "
        "expire after {asr_hours} hours. Raw search phrases are not written to "
        "the journal. User profile data expires after {user_days} days of "
        "inactivity. Use /mydata to inspect it and /delete_me to erase it.\n\n"
        "Full policy: <a href=\"{url}\">{url}</a>"
    ),
    "mydata_empty": "I have no durable data linked to your Telegram id.",
    "mydata_text": (
        "🗂 <b>Your stored data</b>\n\n"
        "Language: {language}\nTerms accepted: {terms}\n"
        "Recent episodes: {recents}\nJournal events: {events}\n"
        "ASR queue rows: {asr_jobs}\n\nUse /delete_me to erase it."
    ),
    "delete_me_confirm": (
        "🗑 <b>Delete your data?</b>\n\nThis removes your language, Terms "
        "acceptance, recent list, event rows and transcription-queue rows. "
        "It cannot retract clips already sent to Telegram.\n\n"
        "Send <code>/delete_me confirm</code> to continue."
    ),
    "delete_me_done": (
        "✅ Your durable user data was deleted. You will need to accept the "
        "Terms again before using the bot."
    ),
    "copyright_text": (
        "©️ <b>Rights and takedown requests</b>\n\n"
        "Podcast Cutter is an independent user-directed tool. If material "
        "should be blocked, send the podcast/episode URL, the right you "
        "represent and enough information to identify the work to: {contact}. "
        "Do not include unrelated personal data in a public issue."
    ),

    # -- the language chooser ------------------------------------------
    "language_screen": (
        "🌐 <b>Language / Язык</b>\n\n"
        "Choose the language I should answer in.\n"
        "Выберите язык, на котором мне отвечать."
    ),
    "language_set": "✅ Answering in English from here on.",

    # -- rate limits and busy states ------------------------------------
    "rate_input": (
        "🐢 That is a lot of requests in one minute. "
        "Give it a moment and try again."
    ),
    "rate_cuts": (
        "🐢 That was a lot of clips for one hour. "
        "The budget resets as the hour rolls on — try again soon."
    ),
    "busy_running": "One job at a time, please — this one is still running.",
    "busy_previous_clip": "⏳ Still working on your previous clip — one at a time!",
    "asr_budget_spent": (
        "🐢 That is the day's budget for first listens spent. "
        "Episodes the bot already knows still search instantly."
    ),
    "asr_queue_full": (
        "The listening queue is full right now — try again in a few minutes."
    ),

    # -- cutting: refusals, progress, delivery ---------------------------
    "circle_rule": (
        "⚠️ A circle fits one minute — Telegram's rule. Shorten the "
        "clip, or pick 🎬 Video to send it square and full-length."
    ),
    "video_cap": (
        "⚠️ Video is capped at {limit} — shorten the clip or switch back "
        "to audio."
    ),
    "queued_slot": "⏳ Queued — waiting for a free slot…",
    "working_on_it": "⏳ Working on it…",
    "downloading_episode": "⬇️ Downloading the episode…",
    "painting": "🎨 Painting the sound…",
    "uploading": "📤 Uploading {size}…",
    "upload_rejected": (
        "I cut the audio but Telegram refused the upload. Try a shorter clip."
    ),
    "cut_cancelled": "✂️ Cancelled — nothing was sent.",
    "reskin_hint": (
        "Fancy another look? Tap a skin below — I'll re-render this same "
        "clip, nothing to set up again."
    ),
    "full_episode_link": "Full episode",
    "btn_full_episode": "🎧 Open the full episode",
    "btn_demo": "🎨 Demo all skins",
    "demo_working": "🎨 Rendering {label} — {i}/{n}…",
    "demo_done": (
        "🎨 That was every skin on a {seconds}-second sample of your clip. "
        "Pick the one you liked and hit Cut."
    ),
    "status_cutting": "✂️ Cutting the segment…",
    "status_full_download": (
        "⬇️ This host does not allow partial reads — downloading "
        "the full episode first. This can take a couple of minutes…"
    ),
    "status_compressing": "🗜 The segment is large — compressing it…",

    # -- listening to an episode -----------------------------------------
    "getting_ready": "🎧 Getting ready to listen…",
    "estimate_note": (
        "About {duration} for this one — it only happens once per episode."
    ),
    "stage_download": "⬇️ Fetching the episode…",
    "stage_decode": "🔧 Preparing the audio…",
    "stage_transcribe": "🎧 Listening to the episode…",
    "stage_index": "🧭 Indexing what was said…",
    "stage_working": "🎧 Working…",
    "queue_position": (
        "⏳ <b>{place} in line</b>\n\n"
        "<i>{episodes} ahead of this one. "
        "Listening starts on its own — nothing to press.</i>"
    ),
    "about_left": "about {duration} left",
    "waiting_notes": (
        "This happens once per episode — every later search on it is instant.",
        "The whole episode gets listened to in one go, so any future question "
        "about it is already paid for.",
        "Timestamps come from the words themselves, so a clip opens where the "
        "phrase actually starts.",
        "Silence and music are skipped, which is why the bar sometimes jumps.",
        "Names and jargon are the hard part; common words come out fine.",
        "Once this is done you can search this episode as many times as you like.",
    ),
    "that_episode": "that episode",
    "notify_failed": (
        "⚠️ I could not finish listening to <b>{title}</b>. "
        "Ask again and I will retry it."
    ),
    "notify_done": (
        "✅ I have finished listening to <b>{title}</b> — "
        "searching inside it is instant now.\n\n"
        "<i>Your place in the chat was lost when I restarted, so open "
        "it again and ask for the phrase.</i>"
    ),

    # -- inline mode ------------------------------------------------------
    "inline_open_bot": "Open Podcast Cutter",
    "inline_cut_link": "✂️ Cut a clip from this",

    # -- transient "working" lines ----------------------------------------
    "working_search": "🔍 Searching “{query}”…",
    "working_person": "🔎 Searching “{query}”…",
    "working_trending": "🔥 Fetching trending…",
    "working_surprise": "🎲 Picking an episode…",
    "working_episodes": "🎧 Loading episodes…",

    # -- breadcrumbs -------------------------------------------------------
    "crumb_search": "🔍 Search",
    "crumb_person": "🔎 Episodes",
    "crumb_trending": "🔥 Trending",
    "crumb_recent": "🕘 Recent",

    # -- screens -----------------------------------------------------------
    "menu_screen": (
        "🎙 <b>Podcast Cutter</b>\n"
        "Find an episode, pick a moment, get just that part.\n\n"
        "Tap below, or just send me a podcast name."
    ),
    "ask_podcast": "🔍 <b>Which podcast?</b>\n\nSend me its name.",
    "ask_person": (
        "🔎 <b>Who or what?</b>\n\n"
        "I'll search episodes across every podcast in the directory — a "
        "guest's name, a topic, or an episode's title."
    ),
    "feeds_found": (
        "Found <b>{n}</b> on this page. Pick one, or send a different name."
    ),
    "episodes_heading": (
        "🎧 <b>{n}</b> {episodes}. Pick one, or type part of a title to filter."
    ),
    "filter_match": "🔎 <b>{n}</b> of {total} match “{query}”.",
    "filter_none": "🔎 Nothing matches “{query}”. Try other words.",
    "global_heading": "🔎 <b>{n}</b> {episodes} match.",
    "trending_heading": "🔥 <b>Popular right now.</b>",
    "recent_heading": "🕘 <b>Episodes you looked at recently.</b>",
    "recent_empty": (
        "🕘 Nothing here yet — episodes you cut show up in this list."
    ),
    "interval_editor": (
        "✂️ <b>{start} → {end}</b>   <i>({length})</i>\n\n"
        "Send a timestamp to jump there — <code>12:30</code> for a "
        "{length} clip, or <code>12:30-14:00</code> for an exact range."
    ),
    "back_to_moments": (
        "‹ Back returns to the {n} {moments} found for “{phrase}”."
    ),
    "ask_phrase": (
        "🔎 <b>What was said?</b>\n\n"
        "Send a word or a phrase and I'll find where it comes up.\n\n"
    ),
    "promise_instant": "This episode is already transcribed, so this is instant.",
    "promise_first": (
        "⏳ Nobody has searched this episode yet, so I'll listen to it "
        "first. That takes a few minutes — you'll see progress, and it "
        "only happens once per episode."
    ),
    "moments_none": (
        "🔎 <b>“{query}”</b>\n\n"
        "Nothing in this episode matches that.\n\n"
        "It may not have been said — or it was said and misheard: "
        "transcription is imperfect on names and jargon."
    ),
    "moments_header": "🔎 <b>“{query}”</b> — {n} {moments}",
    "moments_tap": "Tap a number to open the clip editor there.",
    "result_sent": (
        "✅ Sent <b>{start} → {end}</b>\n\n"
        "Not quite the right moment? Nudge it below."
    ),

    # -- the /stats panel --------------------------------------------------
    "stats_24h": "Last 24h",
    "stats_7d": "Last 7 days",
    "stats_clips": "  clips: {ok} ok · {failed} failed  ({rate})",
    "stats_people": "  people: {n}",
    "stats_time": "  time: {median}s median · {worst}s worst",
    "stats_voice": "  voice notes: {share}% · sent {size}",
    "stats_failures": "Failures this week",
    "stats_top": "Most cut this week",
    "stats_sources": "Where people came from this week",
    "stats_activity": "Activity",
    "stats_journal": "journal: {size}",

    # -- reply-keyboard buttons -------------------------------------------
    "btn_search_podcast": "🔍 Search a podcast",
    "btn_search_person": "🔎 Find episodes",
    "btn_trending": "🔥 Trending",
    "btn_surprise": "🎲 Surprise me",
    "btn_recent": "🕘 Recent",
    "btn_help": "❓ Help",
    "btn_accept_terms": "✅ I agree",
    "btn_decline_terms": "Decline",

    # -- inline buttons ----------------------------------------------------
    "btn_back": "‹ Back",
    "btn_menu": "☰ Menu",
    "btn_cancel": "✕ Cancel",
    "btn_clear_filter": "✕ Clear filter",
    "btn_cut": "✂️ Cut it",
    "btn_find": "🔎 Find a moment by what was said",
    "btn_search_again": "🔎 Search again",
    "btn_earlier": "↺ 15s earlier",
    "btn_later": "15s later ↻",
    "btn_another_clip": "✂️ Another clip from this episode",
    "btn_share": "📤 Share this episode",
    "btn_open": "🎧 Open it",
    "btn_retry": "↻ Try again",
    "btn_menu_podcast": "🔍 Podcast",
    "btn_menu_person": "🔎 Episodes",
    "btn_menu_trending": "🔥 Trending",
    "btn_menu_surprise": "🎲 Surprise",
    "btn_menu_recent": "🕘 Recent episodes",
    "btn_menu_help": "❓ How this works",
    "btn_menu_language": "🌐 Language",

    # -- clip-editor labels ------------------------------------------------
    "fmt_audio": "🎵 Audio",
    "fmt_voice": "🎤 Voice",
    "fmt_note": "⭕ Circle",
    "fmt_video": "🎬 Video",
    "skin_cover": "🖼 Cover",
    "skin_vinyl": "💿 Vinyl",
    "skin_random": "🎲 Random",
    "skin_subway": "🏄 Subway",
    "skin_aurora": "🌌 Aurora",
    "skin_party": "🪩 Party",
    "skin_lava": "🌋 Lava",
    "skin_matrix": "💊 Matrix",
    "skin_fractal": "🌀 Fractal",
    "skin_dvd": "📀 DVD",
    "unit_minutes": "m",
    "unit_seconds": "s",
    "untitled": "Untitled",

    # -- small formatters --------------------------------------------------
    "byte_units": ("B", "KB", "MB", "GB"),
    "so_far": "{amount} so far",

    # -- the bot's Telegram profile ---------------------------------------
    "cmd_search": "Find a podcast by name",
    "cmd_person": "Find episodes — a guest, a topic, a title",
    "cmd_trending": "What is popular right now",
    "cmd_surprise": "A random episode",
    "cmd_recent": "Episodes you looked at",
    "cmd_language": "Language / Язык",
    "cmd_cancel": "Back to the main menu",
    "cmd_reset": "Start over if something looks stuck",
    "cmd_help": "How this works",
    "cmd_terms": "Terms of Use",
    "cmd_privacy": "Privacy Policy and stored data",
    "cmd_mydata": "Show my stored data",
    "cmd_delete_me": "Delete my stored data",
    "cmd_copyright": "Rights and takedown requests",
    "short_description": (
        "Turns a podcast episode into a short clip you can send to someone — "
        "pick the moment, share just that part."
    ),
    "description": (
        "I make shareable clips out of podcasts.\n\n"
        "Find an episode by the podcast's name or by who is in it, tell me when "
        "the good part starts — 12:30, or 12:30-14:00 for an exact range — and I "
        "send that piece back as an audio file or a voice note.\n\n"
        "Works without opening me, too: type @{username} and a name in any chat "
        "to hand someone an episode mid-conversation.\n\n"
        "Press START and send me a podcast name."
    ),

    # -- errors ------------------------------------------------------------
    "err_generic": "Something went wrong. Please try again.",
    "err_misconfigured": "The bot is misconfigured.",
    "err_directory": "The podcast directory is unavailable right now.",
    "err_not_found": "Nothing found.",
    "err_audio": "Could not cut this episode.",
    "err_blocked": (
        "The host of this episode refuses downloads from this server. "
        "This usually means Spotify-hosted feeds; try a different episode."
    ),
    "err_unsafe": "This episode's audio link was refused for security reasons.",
    "err_unsafe_scheme": (
        "This episode's audio link is not an ordinary web download, "
        "so it was not opened."
    ),
    "err_unsafe_no_host": "This episode's audio link has no host in it.",
    "err_unsafe_private": (
        "This episode's audio link points inside this server's own "
        "network, so it was not opened."
    ),
    "err_unreachable": "The episode's audio file could not be reached.",
    "err_unreadable": (
        "Could not cut this episode — the audio file appears to be unreadable."
    ),
    "err_timeout": (
        "Audio processing took too long and was stopped. "
        "Try a shorter interval or a different episode."
    ),
    "err_bad_interval": "That interval does not look right.",
    "err_too_large": "The cut is too large to send.",
    "err_asr_disabled": (
        "Searching inside episodes is switched off right now. "
        "You can still cut by timestamp."
    ),
    "err_api_malformed": "The podcast directory returned a malformed response.",
    "err_api_auth": "The bot cannot authenticate with the podcast directory.",
    "err_api_rate": "Rate limited by the podcast directory.",
    "err_api_status": "The podcast directory returned {status}.",
    "err_api_down": (
        "The podcast directory is not responding. "
        "Please try again in a moment."
    ),
    "err_no_podcasts": "No podcasts found for “{query}”.",
    "err_no_more_pages": "No more podcasts on that page.",
    "err_no_episodes": "This podcast has no downloadable episodes.",
    "err_no_person": "No episodes found for “{query}”.",
    "err_episode_gone": "That episode is no longer available.",
    "err_content_blocked": (
        "This podcast is unavailable for processing following a rights or "
        "policy request."
    ),
    "err_user_data_deleted": "Your pending request was stopped and its data deleted.",
    "err_no_trending": "No trending podcasts right now.",
    "err_no_random": "Could not find a random episode. Try again.",
    "err_ts_missing": "A timestamp is missing.",
    "err_ts_invalid": (
        "“{raw}” is not a valid timestamp. Use MM:SS or HH:MM:SS."
    ),
    "err_ts_over59": "“{raw}” has a minute or second value above 59.",
    "err_ts_unparsed": (
        "“{raw}” is not a valid timestamp. Try 01:20, 1:05:00 or 90s."
    ),
    "err_range_format": (
        "Send a start and an end separated by a hyphen, e.g. 01:20-02:00."
    ),
    "err_end_before_start": "The end time must come after the start time.",
    "err_interval_too_long": "That is {duration} long. The maximum is {max}.",
    "err_no_audio_link": "This episode has no usable audio link.",
    "err_source_too_big": (
        "This episode file is unusually large; refusing to download it."
    ),
    "err_host_status": "The episode host returned {status}.",
    "err_download_failed": "Could not download the episode: {reason}",
    "err_empty_file": "The episode host returned an empty file.",
    "err_episode_too_long": (
        "This episode is {duration} long, past the {max} this bot will "
        "open. Try a shorter episode."
    ),
    "err_past_end": (
        "This episode is only {duration} long, so {start} is past the end."
    ),
    "err_cut_too_large": (
        "The cut is {size} MB, above the {limit} MB Telegram limit. "
        "Please pick a shorter interval."
    ),
    "err_video_too_large": (
        "The video is {size} MB, above the {limit} MB Telegram limit. "
        "Please pick a shorter interval."
    ),
    "err_episode_too_long_asr": "This episode is too long to transcribe.",
    "err_decode_failed": (
        "Could not decode this episode's audio for transcription."
    ),
    "err_render_failed": "Could not render the video for this clip.",
}

# ---------------------------------------------------------------------------
# Russian
# ---------------------------------------------------------------------------

_RU = {
    "welcome": (
        "👋 <b>Привет!</b>\n\n"
        "Я вырезаю короткий фрагмент из эпизода подкаста и присылаю его "
        "обратно — чтобы можно было поделиться самым интересным, а не "
        "двухчасовой ссылкой.\n\n"
        "Когда выберете эпизод, скажите, где начинается нужное место: "
        "<code>12:30</code> — клип оттуда, или <code>12:30-14:00</code> — "
        "точный диапазон. Кнопки ◀ ▶ подвинут его, пока не станет точно.\n\n"
        "В любом другом чате наберите <code>@{username}</code> и название — "
        "и передадите собеседнику эпизод, не выходя из разговора.\n\n"
        "Пришлите название подкаста, чтобы начать."
    ),
    "help": (
        "🎙 <b>Podcast Cutter</b>\n\n"
        "Найдите эпизод, назовите момент — получите обратно только его.\n\n"
        "<b>Как искать</b>\n"
        "/search — подкаст по названию\n"
        "/person — поиск эпизодов: гость, тема, название\n"
        "/trending — что популярно\n"
        "/surprise — случайный эпизод\n"
        "/recent — эпизоды, которые вы открывали\n"
        "/language — язык / language\n"
        "/terms · /privacy — правила и сохранённые данные\n"
        "/mydata · /delete_me — посмотреть или удалить данные\n"
        "/copyright — обращения о правах и блокировке\n"
        "/cancel — в главное меню\n"
        "/help — это сообщение\n\n"
        "<b>Как выбрать момент</b>\n"
        "Пришлите <code>12:30</code> — клип с этого места, или "
        "<code>12:30-14:00</code> — точный диапазон.\n"
        "Потом подвиньте кнопками ◀ ▶, пока не станет точно.\n\n"
        "<b>Где угодно ещё</b>\n"
        "Наберите <code>@{username}</code> в любом чате, чтобы поделиться "
        "эпизодом, не выходя из разговора.\n\n"
        "<i>Независимый инструмент · данные каталога через Podcast Index.</i>"
    ),
    "unknown_command": "Такой команды я не знаю — /help перечисляет те, что есть.",
    "opened_from_link": "🔗 Открыто по ссылке.",
    "no_journal": "Журнал не настроен.",
    "started_fresh": (
        "⏱ Прошло много времени, и я начал заново — если это относилось "
        "к прежнему экрану, просто откройте его ещё раз."
    ),
    "stale_menu": "⌛ Это меню устарело — вот свежее.",
    "generic_error": "Что-то пошло не так на моей стороне. Попробуйте ещё раз.",
    "terms_prompt": (
        "⚖️ <b>Перед использованием Podcast Cutter</b>\n\n"
        "Вы сами выбираете материал и поручаете боту его обработать. Продолжая, "
        "вы подтверждаете, что имеете разрешение или иное законное основание "
        "создать и передать каждый клип, и отвечаете за его использование. "
        "Пиратство и нарушение чужих прав запрещены.\n\n"
        "<a href=\"{terms_url}\">Условия использования</a> · "
        "<a href=\"{privacy_url}\">Политика конфиденциальности</a>\n"
        "Версия: {version}"
    ),
    "terms_accepted": "✅ Условия приняты. Теперь ботом можно пользоваться.",
    "terms_declined": (
        "Хорошо — без принятия условий бот не будет искать, загружать или "
        "обрабатывать материалы. Команды /terms и /privacy остаются доступны."
    ),
    "terms_text": (
        "⚖️ <b>Условия использования</b>\n\n"
        "Можно обрабатывать только материалы, на которые у вас есть права, "
        "разрешение или иное законное основание. Вы выбираете источник, "
        "интервал и получателя и отвечаете за полученный клип и его "
        "распространение. Запрещены незаконное использование, нарушение прав, "
        "обман и коммерческое использование без необходимых прав. Доступ может "
        "быть ограничен, а материал — заблокирован или удалён. Сервис независим, "
        "предоставляется как есть и не одобрен Podcast Index или авторами.\n\n"
        "Полный текст: <a href=\"{url}\">{url}</a>"
    ),
    "privacy_text": (
        "🔐 <b>Конфиденциальность</b>\n\n"
        "Хранятся Telegram ID, выбранный язык, принятие условий, недавние "
        "эпизоды и ограниченный журнал работы. Chat ID и URL эпизода хранятся, "
        "пока выполняется расшифровка; завершённые строки очереди удаляются "
        "через {asr_hours} ч. Исходные поисковые фразы в журнал не записываются. "
        "Профиль удаляется после {user_days} дней неактивности. /mydata покажет "
        "данные, /delete_me удалит их.\n\n"
        "Политика полностью: <a href=\"{url}\">{url}</a>"
    ),
    "mydata_empty": "У меня нет постоянных данных, связанных с вашим Telegram ID.",
    "mydata_text": (
        "🗂 <b>Ваши сохранённые данные</b>\n\n"
        "Язык: {language}\nУсловия приняты: {terms}\n"
        "Недавние эпизоды: {recents}\nСобытия журнала: {events}\n"
        "Строки очереди ASR: {asr_jobs}\n\n/delete_me удалит эти данные."
    ),
    "delete_me_confirm": (
        "🗑 <b>Удалить ваши данные?</b>\n\nБудут удалены язык, принятие "
        "условий, список недавнего, события журнала и строки очереди "
        "расшифровки. Уже отправленные в Telegram клипы отозвать нельзя.\n\n"
        "Для продолжения отправьте <code>/delete_me confirm</code>."
    ),
    "delete_me_done": (
        "✅ Ваши постоянные пользовательские данные удалены. Перед следующим "
        "использованием потребуется снова принять условия."
    ),
    "copyright_text": (
        "©️ <b>Права и удаление материалов</b>\n\n"
        "Podcast Cutter — независимый инструмент, которым управляет пользователь. "
        "Чтобы заблокировать материал, отправьте ссылку на подкаст или эпизод, "
        "укажите представляемое право и сведения для идентификации произведения: "
        "{contact}. Не публикуйте лишние персональные данные в открытом issue."
    ),

    "language_screen": (
        "🌐 <b>Language / Язык</b>\n\n"
        "Choose the language I should answer in.\n"
        "Выберите язык, на котором мне отвечать."
    ),
    "language_set": "✅ Готово — теперь отвечаю по-русски.",

    "rate_input": (
        "🐢 Многовато запросов за одну минуту. "
        "Подождите немного и попробуйте снова."
    ),
    "rate_cuts": (
        "🐢 Многовато клипов за один час. "
        "Лимит восстанавливается по ходу часа — попробуйте чуть позже."
    ),
    "busy_running": "Давайте по одному делу за раз — это ещё выполняется.",
    "busy_previous_clip": (
        "⏳ Ещё режу ваш предыдущий клип — по одному за раз!"
    ),
    "asr_budget_spent": (
        "🐢 Дневной лимит первых прослушиваний исчерпан. "
        "По уже знакомым эпизодам поиск по-прежнему мгновенный."
    ),
    "asr_queue_full": (
        "Очередь на прослушивание сейчас заполнена — "
        "попробуйте через несколько минут."
    ),

    "circle_rule": (
        "⚠️ В кружок помещается одна минута — правило Telegram. Укоротите "
        "клип или выберите 🎬 Видео, чтобы отправить его квадратом любой длины."
    ),
    "video_cap": (
        "⚠️ Видео ограничено {limit} — укоротите клип или вернитесь к аудио."
    ),
    "queued_slot": "⏳ В очереди — жду свободный слот…",
    "working_on_it": "⏳ Работаю…",
    "downloading_episode": "⬇️ Скачиваю эпизод…",
    "painting": "🎨 Рисую звук…",
    "uploading": "📤 Отправляю {size}…",
    "upload_rejected": (
        "Аудио я вырезал, но Telegram отказался его принимать. "
        "Попробуйте клип покороче."
    ),
    "cut_cancelled": "✂️ Отменено — ничего не отправлено.",
    "reskin_hint": (
        "Хочется другой вид? Нажмите скин ниже — я перерисую этот же клип, "
        "ничего заново выбирать не нужно."
    ),
    "full_episode_link": "Полный выпуск",
    "btn_full_episode": "🎧 Открыть полный выпуск",
    "btn_demo": "🎨 Демо всех скинов",
    "demo_working": "🎨 Рендерю {label} — {i}/{n}…",
    "demo_done": (
        "🎨 Это были все скины на {seconds}-секундном кусочке вашего клипа. "
        "Выберите понравившийся и жмите «Вырезать»."
    ),
    "status_cutting": "✂️ Вырезаю фрагмент…",
    "status_full_download": (
        "⬇️ Этот хостинг не отдаёт файл по частям — сначала скачиваю "
        "эпизод целиком. Это может занять пару минут…"
    ),
    "status_compressing": "🗜 Фрагмент получился большим — сжимаю…",

    "getting_ready": "🎧 Готовлюсь слушать…",
    "estimate_note": (
        "Примерно {duration} на этот эпизод — и только в первый раз."
    ),
    "stage_download": "⬇️ Скачиваю эпизод…",
    "stage_decode": "🔧 Готовлю аудио…",
    "stage_transcribe": "🎧 Слушаю эпизод…",
    "stage_index": "🧭 Индексирую сказанное…",
    "stage_working": "🎧 Работаю…",
    "queue_position": (
        "⏳ <b>{place} в очереди</b>\n\n"
        "<i>Впереди {episodes}. "
        "Прослушивание начнётся само — нажимать ничего не нужно.</i>"
    ),
    "about_left": "осталось около {duration}",
    "waiting_notes": (
        "Это происходит один раз на эпизод — все следующие поиски по нему "
        "мгновенные.",
        "Эпизод прослушивается целиком за один заход, так что любой будущий "
        "вопрос по нему уже оплачен.",
        "Таймкоды берутся из самих слов, поэтому клип откроется там, где "
        "фраза действительно начинается.",
        "Тишина и музыка пропускаются — поэтому полоска иногда прыгает.",
        "Имена и термины — самое трудное; обычные слова распознаются хорошо.",
        "Когда это закончится, искать по эпизоду можно будет сколько угодно.",
    ),
    "that_episode": "этот эпизод",
    "notify_failed": (
        "⚠️ Мне не удалось дослушать <b>{title}</b>. "
        "Спросите ещё раз — я попробую снова."
    ),
    "notify_done": (
        "✅ Я дослушал <b>{title}</b> — теперь поиск внутри него "
        "мгновенный.\n\n"
        "<i>Ваше место в чате потерялось при перезапуске, так что откройте "
        "эпизод ещё раз и спросите фразу.</i>"
    ),

    "inline_open_bot": "Открыть Podcast Cutter",
    "inline_cut_link": "✂️ Вырезать клип отсюда",

    "working_search": "🔍 Ищу «{query}»…",
    "working_person": "🔎 Ищу «{query}»…",
    "working_trending": "🔥 Загружаю популярное…",
    "working_surprise": "🎲 Выбираю эпизод…",
    "working_episodes": "🎧 Загружаю эпизоды…",

    "crumb_search": "🔍 Поиск",
    "crumb_person": "🔎 Эпизоды",
    "crumb_trending": "🔥 Популярное",
    "crumb_recent": "🕘 Недавние",

    "menu_screen": (
        "🎙 <b>Podcast Cutter</b>\n"
        "Найдите эпизод, выберите момент — получите только его.\n\n"
        "Нажмите кнопку ниже или просто пришлите название подкаста."
    ),
    "ask_podcast": "🔍 <b>Какой подкаст?</b>\n\nПришлите его название.",
    "ask_person": (
        "🔎 <b>Кто или что?</b>\n\n"
        "Поищу эпизоды по всем подкастам каталога — имя гостя, тема "
        "или название выпуска."
    ),
    "feeds_found": (
        "На этой странице нашлось <b>{n}</b>. Выберите один "
        "или пришлите другое название."
    ),
    "episodes_heading": (
        "🎧 <b>{n}</b> {episodes}. Выберите один или наберите часть "
        "названия, чтобы отфильтровать."
    ),
    "filter_match": "🔎 <b>{n}</b> из {total} подходят под «{query}».",
    "filter_none": "🔎 Ничего не подходит под «{query}». Попробуйте другие слова.",
    "global_heading": "🔎 Нашлось в <b>{n}</b> {episodes}.",
    "trending_heading": "🔥 <b>Популярно прямо сейчас.</b>",
    "recent_heading": "🕘 <b>Эпизоды, которые вы недавно открывали.</b>",
    "recent_empty": (
        "🕘 Пока пусто — здесь появятся эпизоды, из которых вы резали клипы."
    ),
    "interval_editor": (
        "✂️ <b>{start} → {end}</b>   <i>({length})</i>\n\n"
        "Пришлите таймкод, чтобы перейти туда — <code>12:30</code> для клипа "
        "на {length}, или <code>12:30-14:00</code> для точного диапазона."
    ),
    "back_to_moments": (
        "‹ Назад вернёт к {n} {moments}, найденным по «{phrase}»."
    ),
    "ask_phrase": (
        "🔎 <b>Что было сказано?</b>\n\n"
        "Пришлите слово или фразу — я найду, где это звучит.\n\n"
    ),
    "promise_instant": "Этот эпизод уже расшифрован, так что поиск мгновенный.",
    "promise_first": (
        "⏳ Этот эпизод ещё никто не искал, так что сначала я его послушаю. "
        "Это займёт несколько минут — прогресс будет виден, и это "
        "происходит только один раз на эпизод."
    ),
    "moments_none": (
        "🔎 <b>«{query}»</b>\n\n"
        "В этом эпизоде ничего похожего нет.\n\n"
        "Может, это не прозвучало — или прозвучало и было расслышано "
        "неверно: расшифровка несовершенна на именах и терминах."
    ),
    "moments_header": "🔎 <b>«{query}»</b> — {n} {moments}",
    "moments_tap": "Нажмите номер, чтобы открыть редактор клипа в этом месте.",
    "result_sent": (
        "✅ Отправлено: <b>{start} → {end}</b>\n\n"
        "Момент не совсем тот? Подвиньте его кнопками ниже."
    ),

    "stats_24h": "Последние 24 часа",
    "stats_7d": "Последние 7 дней",
    "stats_clips": "  клипы: {ok} ок · {failed} с ошибкой  ({rate})",
    "stats_people": "  люди: {n}",
    "stats_time": "  время: медиана {median}с · худшее {worst}с",
    "stats_voice": "  голосовые: {share}% · отправлено {size}",
    "stats_failures": "Ошибки за неделю",
    "stats_top": "Чаще всего резали за неделю",
    "stats_sources": "Откуда пришли за неделю",
    "stats_activity": "Активность",
    "stats_journal": "журнал: {size}",

    "btn_search_podcast": "🔍 Найти подкаст",
    "btn_search_person": "🔎 Поиск эпизодов",
    "btn_trending": "🔥 Популярное",
    "btn_surprise": "🎲 Удиви меня",
    "btn_recent": "🕘 Недавние",
    "btn_help": "❓ Помощь",
    "btn_accept_terms": "✅ Принимаю",
    "btn_decline_terms": "Отказаться",

    "btn_back": "‹ Назад",
    "btn_menu": "☰ Меню",
    "btn_cancel": "✕ Отмена",
    "btn_clear_filter": "✕ Сбросить фильтр",
    "btn_cut": "✂️ Вырезать",
    "btn_find": "🔎 Найти момент по словам",
    "btn_search_again": "🔎 Искать ещё",
    "btn_earlier": "↺ На 15с раньше",
    "btn_later": "На 15с позже ↻",
    "btn_another_clip": "✂️ Ещё клип из этого эпизода",
    "btn_share": "📤 Поделиться эпизодом",
    "btn_open": "🎧 Открыть",
    "btn_retry": "↻ Попробовать ещё раз",
    "btn_menu_podcast": "🔍 Подкаст",
    "btn_menu_person": "🔎 Эпизоды",
    "btn_menu_trending": "🔥 Популярное",
    "btn_menu_surprise": "🎲 Сюрприз",
    "btn_menu_recent": "🕘 Недавние эпизоды",
    "btn_menu_help": "❓ Как это работает",
    "btn_menu_language": "🌐 Язык",

    "fmt_audio": "🎵 Аудио",
    "fmt_voice": "🎤 Голосовое",
    "fmt_note": "⭕ Кружок",
    "fmt_video": "🎬 Видео",
    "skin_cover": "🖼 Обложка",
    "skin_vinyl": "💿 Винил",
    "skin_random": "🎲 Рандом",
    "skin_subway": "🏄 Сабвей",
    "skin_aurora": "🌌 Сияние",
    "skin_party": "🪩 Вечеринка",
    "skin_lava": "🌋 Лава",
    "skin_matrix": "💊 Матрица",
    "skin_fractal": "🌀 Фрактал",
    "skin_dvd": "📀 DVD",
    "unit_minutes": "м",
    "unit_seconds": "с",
    "untitled": "Без названия",

    "byte_units": ("Б", "КБ", "МБ", "ГБ"),
    "so_far": "уже {amount}",

    "cmd_search": "Найти подкаст по названию",
    "cmd_person": "Поиск эпизодов: гость, тема, название",
    "cmd_trending": "Что сейчас популярно",
    "cmd_surprise": "Случайный эпизод",
    "cmd_recent": "Эпизоды, которые вы открывали",
    "cmd_language": "Язык / Language",
    "cmd_cancel": "Вернуться в главное меню",
    "cmd_reset": "Начать заново, если что-то зависло",
    "cmd_help": "Как это работает",
    "cmd_terms": "Условия использования",
    "cmd_privacy": "Конфиденциальность и данные",
    "cmd_mydata": "Показать мои данные",
    "cmd_delete_me": "Удалить мои данные",
    "cmd_copyright": "Права и удаление материалов",
    "short_description": (
        "Превращает эпизод подкаста в короткий клип: выберите момент — "
        "и поделитесь только им."
    ),
    "description": (
        "Я делаю из подкастов клипы, которыми можно поделиться.\n\n"
        "Найдите эпизод по названию подкаста или по имени гостя, скажите, "
        "где начинается нужное место — 12:30, или 12:30-14:00 для точного "
        "диапазона, — и я пришлю этот кусок аудиофайлом или голосовым "
        "сообщением.\n\n"
        "Работает и в других чатах: наберите @{username} и название, чтобы "
        "передать собеседнику эпизод прямо в разговоре.\n\n"
        "Нажмите START и пришлите название подкаста."
    ),

    "err_generic": "Что-то пошло не так. Попробуйте ещё раз.",
    "err_misconfigured": "Бот настроен неправильно.",
    "err_directory": "Каталог подкастов сейчас недоступен.",
    "err_not_found": "Ничего не нашлось.",
    "err_audio": "Не получилось вырезать фрагмент из этого эпизода.",
    "err_blocked": (
        "Хостинг этого эпизода не отдаёт файлы этому серверу. Обычно так "
        "ведут себя фиды на хостинге Spotify — попробуйте другой эпизод."
    ),
    "err_unsafe": (
        "Аудио-ссылка этого эпизода отклонена из соображений безопасности."
    ),
    "err_unsafe_scheme": (
        "Аудио-ссылка этого эпизода — не обычная веб-загрузка, "
        "поэтому я не стал её открывать."
    ),
    "err_unsafe_no_host": "В аудио-ссылке этого эпизода нет имени хоста.",
    "err_unsafe_private": (
        "Аудио-ссылка этого эпизода ведёт во внутреннюю сеть сервера, "
        "поэтому не была открыта."
    ),
    "err_unreachable": "Не удалось получить аудиофайл эпизода.",
    "err_unreadable": (
        "Не получилось вырезать фрагмент — аудиофайл не читается."
    ),
    "err_timeout": (
        "Обработка аудио заняла слишком много времени и была остановлена. "
        "Попробуйте более короткий интервал или другой эпизод."
    ),
    "err_bad_interval": "Не могу разобрать этот интервал.",
    "err_too_large": "Фрагмент получился слишком большим для отправки.",
    "err_asr_disabled": (
        "Поиск внутри эпизодов сейчас выключен. "
        "Вырезать по таймкоду по-прежнему можно."
    ),
    "err_api_malformed": "Каталог подкастов вернул некорректный ответ.",
    "err_api_auth": "Бот не может авторизоваться в каталоге подкастов.",
    "err_api_rate": "Каталог подкастов ограничил частоту запросов.",
    "err_api_status": "Каталог подкастов ответил кодом {status}.",
    "err_api_down": "Каталог подкастов не отвечает. Попробуйте чуть позже.",
    "err_no_podcasts": "По запросу «{query}» подкастов не нашлось.",
    "err_no_more_pages": "На этой странице подкастов больше нет.",
    "err_no_episodes": "У этого подкаста нет эпизодов, доступных для скачивания.",
    "err_no_person": "По запросу «{query}» эпизодов не нашлось.",
    "err_episode_gone": "Этот эпизод больше недоступен.",
    "err_content_blocked": (
        "Этот подкаст недоступен для обработки после обращения о правах "
        "или требования политики."
    ),
    "err_user_data_deleted": "Ожидающий запрос остановлен, а его данные удалены.",
    "err_no_trending": "Списка популярных подкастов сейчас нет.",
    "err_no_random": "Не удалось найти случайный эпизод. Попробуйте ещё раз.",
    "err_ts_missing": "Не хватает таймкода.",
    "err_ts_invalid": (
        "«{raw}» не похоже на таймкод. Используйте ММ:СС или ЧЧ:ММ:СС."
    ),
    "err_ts_over59": "В «{raw}» минуты или секунды больше 59.",
    "err_ts_unparsed": (
        "«{raw}» не похоже на таймкод. Попробуйте 01:20, 1:05:00 или 90s."
    ),
    "err_range_format": (
        "Пришлите начало и конец через дефис, например 01:20-02:00."
    ),
    "err_end_before_start": "Время конца должно быть позже времени начала.",
    "err_interval_too_long": "Это {duration}. Максимум — {max}.",
    "err_no_audio_link": "У этого эпизода нет пригодной аудио-ссылки.",
    "err_source_too_big": (
        "Файл этого эпизода необычно большой — не буду его скачивать."
    ),
    "err_host_status": "Хостинг эпизода ответил кодом {status}.",
    "err_download_failed": "Не удалось скачать эпизод: {reason}",
    "err_empty_file": "Хостинг эпизода вернул пустой файл.",
    "err_episode_too_long": (
        "Этот эпизод длится {duration} — дольше {max}, с которыми я "
        "работаю. Попробуйте эпизод покороче."
    ),
    "err_past_end": (
        "Этот эпизод длится всего {duration}, так что {start} — уже за "
        "его концом."
    ),
    "err_cut_too_large": (
        "Фрагмент получился {size} МБ — больше лимита Telegram в {limit} МБ. "
        "Выберите интервал покороче."
    ),
    "err_video_too_large": (
        "Видео получилось {size} МБ — больше лимита Telegram в {limit} МБ. "
        "Выберите интервал покороче."
    ),
    "err_episode_too_long_asr": "Этот эпизод слишком длинный для расшифровки.",
    "err_decode_failed": "Не удалось декодировать аудио эпизода для расшифровки.",
    "err_render_failed": "Не удалось отрисовать видео для этого клипа.",
}

_STRINGS: dict[str, dict] = {"en": _EN, "ru": _RU}

# ---------------------------------------------------------------------------
# Plural forms
# ---------------------------------------------------------------------------

#: Noun forms, indexed by :func:`_plural_index` — English has (one, many),
#: Russian has (1, 2–4, 5+) with the teens as 5+.
_PLURALS = {
    "en": {
        "episodes": ("episode", "episodes"),
        "moments": ("moment", "moments"),
    },
    "ru": {
        "episodes": ("эпизод", "эпизода", "эпизодов"),
        "moments": ("момент", "момента", "моментов"),
    },
}

#: Case variants Russian needs and English does not: "in N episodes"
#: («в 1 эпизоде / в 5 эпизодах») and "back to N moments" («к 3 моментам»).
_PLURALS["en"]["episodes_in"] = _PLURALS["en"]["episodes"]
_PLURALS["ru"]["episodes_in"] = ("эпизоде", "эпизодах", "эпизодах")
_PLURALS["en"]["moments_to"] = _PLURALS["en"]["moments"]
_PLURALS["ru"]["moments_to"] = ("моменту", "моментам", "моментам")


def _plural_index(lang: str, n: int) -> int:
    n = abs(int(n))
    if lang == "ru":
        if n % 100 in range(11, 15):
            return 2
        if n % 10 == 1:
            return 0
        if n % 10 in (2, 3, 4):
            return 1
        return 2
    return 0 if n == 1 else 1


def plural(lang: str, key: str, n: int) -> str:
    """The right form of a counted noun — the word only, not the number."""
    table = _PLURALS.get(lang)
    if table is None or key not in table:
        lang, table = DEFAULT_LANGUAGE, _PLURALS[DEFAULT_LANGUAGE]
    forms = table[key]
    return forms[min(_plural_index(lang, n), len(forms) - 1)]


def ordinal(lang: str, n: int) -> str:
    """``3`` → ``3rd`` / ``3-й``, for queue positions."""
    if lang == "ru":
        return f"{n}-й"
    if n % 100 in (11, 12, 13):
        return f"{n}th"
    return {1: f"{n}st", 2: f"{n}nd", 3: f"{n}rd"}.get(n % 10, f"{n}th")


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


def t(lang: str, key: str, **params) -> str:
    """The sentence under ``key`` in ``lang``, with placeholders filled in.

    Falls back to English for an untranslated key — the parity test makes that
    unreachable in a release, but a fallback beats a crash in the field.
    """
    table = _STRINGS.get(lang, _EN)
    template = table.get(key)
    if template is None:
        template = _EN[key]
    return template.format(**params) if params else template


def t_seq(lang: str, key: str) -> tuple[str, ...]:
    """A tuple-valued entry, e.g. the waiting notes or the byte units."""
    table = _STRINGS.get(lang, _EN)
    value = table.get(key)
    if value is None:
        value = _EN[key]
    return value


def is_message_key(text: str) -> bool:
    """Whether ``text`` names an entry here rather than being a literal."""
    return text in _EN


def resolve_message(lang: str, key_or_text: str, params: dict) -> str:
    """A key becomes its translation; anything else passes through verbatim.

    The pass-through is what lets operator-facing errors — config problems,
    ffmpeg missing from PATH — keep carrying their literal, detailed text
    without every one of them needing a table entry nobody will translate.
    """
    if is_message_key(key_or_text):
        return t(lang, key_or_text, **params)
    return key_or_text


def bot_commands(lang: str) -> list[tuple[str, str]]:
    """The command menu Telegram shows, in one language."""
    return [
        ("search", t(lang, "cmd_search")),
        ("person", t(lang, "cmd_person")),
        ("trending", t(lang, "cmd_trending")),
        ("surprise", t(lang, "cmd_surprise")),
        ("recent", t(lang, "cmd_recent")),
        ("language", t(lang, "cmd_language")),
        ("terms", t(lang, "cmd_terms")),
        ("privacy", t(lang, "cmd_privacy")),
        ("mydata", t(lang, "cmd_mydata")),
        ("delete_me", t(lang, "cmd_delete_me")),
        ("copyright", t(lang, "cmd_copyright")),
        ("cancel", t(lang, "cmd_cancel")),
        ("reset", t(lang, "cmd_reset")),
        ("help", t(lang, "cmd_help")),
    ]

"""Application wiring: handler registration and lifecycle.

Registration is flat on purpose. One callback router, one text router and one
inline handler cover every interaction, so there is no ordering subtlety about
which handler shadows which — the routers themselves decide.
"""

from __future__ import annotations

import logging
import shutil
from logging.handlers import RotatingFileHandler

from telegram import BotCommand, MenuButtonCommands, Update
from telegram.error import TelegramError
from telegram.ext import (
    AIORateLimiter,
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    InlineQueryHandler,
    MessageHandler,
    filters,
)

from .api import PodcastIndexClient
from .asr import build_recognizer
from .audio import ensure_ffmpeg_available
from .config import Settings, load_settings
from .embeddings import build_embedder
from .handlers import PodcastCutterBot
from .indexer import Indexer
from .states import Screen
from .store import Event, Store

logger = logging.getLogger(__name__)

#: Shown in Telegram's own command menu. Without this the commands work but
#: are invisible unless someone reads /help.
BOT_COMMANDS = [
    ("search", "Find a podcast by name"),
    ("person", "Find episodes mentioning someone"),
    ("trending", "What is popular right now"),
    ("surprise", "A random episode"),
    ("recent", "Episodes you looked at"),
    ("cancel", "Back to the main menu"),
    ("reset", "Start over if something looks stuck"),
    ("help", "How this works"),
]

#: Shown in the bot's profile and in the preview of a t.me link — the only
#: words most people read before deciding whether to press Start. Telegram
#: caps it at 120 characters.
SHORT_DESCRIPTION = (
    "Turns a podcast episode into a short clip you can send to someone — "
    "pick the moment, share just that part."
)

#: Shown on the empty chat screen, above the Start button, and nowhere else.
#: Plain text only: Telegram renders no markup here. Capped at 512 characters.
DESCRIPTION = (
    "I make shareable clips out of podcasts.\n\n"
    "Find an episode by the podcast's name or by who is in it, tell me when "
    "the good part starts — 12:30, or 12:30-14:00 for an exact range — and I "
    "send that piece back as an audio file or a voice note.\n\n"
    "Works without opening me, too: type @{username} and a name in any chat "
    "to hand someone an episode mid-conversation.\n\n"
    "Press START and send me a podcast name."
)


LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


def configure_logging() -> None:
    logging.basicConfig(format=LOG_FORMAT, level=logging.INFO)
    # httpx logs a line per request, including every getUpdates poll.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram.ext.Application").setLevel(logging.INFO)


def add_file_logging(settings: Settings) -> None:
    """Also write logs to a rotating file under the data directory.

    Container logs do not survive a redeploy: ``docker compose up --build``
    creates a new container and the old json log is discarded with it. Writing
    to a mounted directory as well is what makes the history outlive a deploy.
    """
    try:
        settings.log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            settings.log_path,
            maxBytes=settings.log_file_bytes,
            backupCount=settings.log_file_count,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        logging.getLogger().addHandler(handler)
        logger.info("Logging to %s", settings.log_path)
    except OSError as exc:
        # A read-only or missing volume must not stop the bot from running.
        logger.warning("File logging disabled: %s", exc)


def sweep_work_dir(settings: Settings) -> None:
    """Delete job directories left behind by a crash or a kill.

    Each cut cleans up after itself, but a hard stop mid-job leaks one
    directory, and nothing else would ever remove it.
    """
    work_dir = settings.work_dir
    if not work_dir.is_dir():
        return

    removed = 0
    for leftover in work_dir.glob("cut-*"):
        if leftover.is_dir():
            shutil.rmtree(leftover, ignore_errors=True)
            removed += 1
    if removed:
        logger.info("Removed %d leftover job director%s",
                    removed, "y" if removed == 1 else "ies")


async def _report_media_proxy(
    bot: PodcastCutterBot, store: Store, settings: Settings
) -> None:
    """Say once, at startup, whether the audio detour works.

    A proxy that is quietly broken looks exactly like no proxy at all: the
    episodes it was added for go back to failing, with the same errors they
    failed with before, and nothing points at the cause. So the answer goes to
    the log and to the journal, and either way the bot starts — degrading to
    direct fetches is the whole design, not an incident.
    """
    proxy = bot.media_proxy
    if not proxy.configured:
        logger.info(
            "MEDIA_PROXY is not configured; audio fetches go direct%s.",
            " (MEDIA_PROXY_MODE=off)" if settings.media_proxy else "",
        )
        return

    alive = await proxy.check()
    if not alive:
        logger.warning(
            "Media proxy %s did not answer at startup: %s. Audio fetches will "
            "go direct until it does.",
            proxy.url,
            proxy.down_reason,
        )
    await store.record(
        Event(
            action="proxy",
            outcome="up" if alive else "down",
            detail=f"{proxy.url} mode={settings.media_proxy_mode}"
            + ("" if alive else f" ({proxy.down_reason})"),
        )
    )


async def _on_startup(application: Application) -> None:
    bot: PodcastCutterBot = application.bot_data["bot"]
    store: Store = application.bot_data["store"]
    settings: Settings = application.bot_data["settings"]

    purged = await store.purge(settings.log_retention_days)
    if purged:
        logger.info(
            "Purged %d journal rows older than %d days",
            purged,
            settings.log_retention_days,
        )

    await _report_media_proxy(bot, store, settings)

    try:
        me = await application.bot.get_me()
        # Needed for the deep links handed out by inline mode.
        bot.bot_username = me.username or ""
    except TelegramError as exc:
        logger.warning("Could not read the bot's own username: %s", exc)

    try:
        await application.bot.set_my_commands(
            [BotCommand(name, description) for name, description in BOT_COMMANDS]
        )
        await application.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except TelegramError as exc:
        # Cosmetic; never worth refusing to start over.
        logger.warning("Could not publish the command list: %s", exc)

    try:
        # Owned by the code, not by @BotFather: whatever is set there is
        # overwritten on the next start. The avatar and the inline placeholder
        # have no API and remain BotFather's alone.
        await application.bot.set_my_short_description(SHORT_DESCRIPTION)
        await application.bot.set_my_description(
            DESCRIPTION.format(username=bot.bot_username or "podcast_cutter_bot")
        )
    except TelegramError as exc:
        logger.warning("Could not publish the bot's description: %s", exc)

    logger.info("Bot started as @%s", bot.bot_username or "unknown")


async def _on_shutdown(application: Application) -> None:
    client: PodcastIndexClient = application.bot_data["api_client"]
    await client.aclose()
    store: Store = application.bot_data["store"]
    await store.aclose()
    logger.info("Bot stopped")


def register_handlers(application: Application, bot: PodcastCutterBot) -> None:
    # /start carries deep-link payloads, so it must see its arguments.
    application.add_handler(CommandHandler("start", bot.cmd_start))
    application.add_handler(CommandHandler("help", bot.cmd_help))
    # /reset is the same full reset under the name people actually reach for
    # when a screen looks stuck; /cancel reads as "abort this one thing".
    application.add_handler(CommandHandler(["cancel", "reset"], bot.cmd_cancel))

    application.add_handler(
        CommandHandler(["search", "cut_podcast"], bot.command(bot.act_ask_podcast))
    )
    application.add_handler(
        CommandHandler(["person", "search_episodes"], bot.command(bot.act_ask_person))
    )
    application.add_handler(CommandHandler("trending", bot.command(bot.act_trending)))
    application.add_handler(CommandHandler("surprise", bot.command(bot.act_surprise)))
    application.add_handler(CommandHandler("recent", bot.command(bot.act_recent)))
    application.add_handler(CommandHandler("menu", bot.command(bot.act_menu)))
    # Admin-only; silently unavailable to everyone else.
    application.add_handler(CommandHandler("stats", bot.cmd_stats))

    application.add_handler(CallbackQueryHandler(bot.on_callback))
    application.add_handler(InlineQueryHandler(bot.on_inline_query))

    # Anything else typed is handled by the text router, which always has
    # somewhere to send it.
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, bot.on_text)
    )
    # Registered last, so it only sees commands nothing above claimed.
    application.add_handler(MessageHandler(filters.COMMAND, bot.cmd_unknown))

    application.add_error_handler(bot.on_error)


def build_indexer(settings: Settings, store: Store) -> Indexer | None:
    """The transcription pipeline, or ``None`` if it cannot run here.

    A missing recognition library must not stop the bot: everything the bot did
    before searching existed still works, and the alternative — refusing to
    start — turns an optional feature into a single point of failure.
    """
    if not settings.asr_enabled:
        logger.info("Transcription is disabled (ASR_ENABLED); search is off.")
        return None
    try:
        recognizer = build_recognizer(settings)
    except Exception:
        logger.exception("Could not set up speech recognition; search is off.")
        return None

    settings.asr_model_dir.mkdir(parents=True, exist_ok=True)
    logger.info(
        "Transcription ready: %s/%s, models in %s",
        settings.asr_backend,
        settings.asr_model,
        settings.asr_model_dir,
    )
    embedder = build_embedder(settings)
    if embedder is not None:
        logger.info("Dense search ready: %s", settings.embed_model_dir)
    return Indexer(settings, store, recognizer, embedder=embedder)


def build_application(settings: Settings, store: Store | None = None) -> Application:
    client = PodcastIndexClient(settings)
    if store is None:
        store = Store(settings.database_path)
        store.connect()
    bot = PodcastCutterBot(settings, client, store, build_indexer(settings, store))

    builder = (
        ApplicationBuilder()
        .token(settings.bot_token)
        # Without this PTB processes updates strictly one at a time, and a
        # first-time transcription — minutes of work — freezes the bot for
        # every user at once. Bounded rather than unlimited so a flood cannot
        # hold arbitrarily many coroutines open; per-user exclusivity is the
        # busy flag in handlers, which is claimed before the first await for
        # exactly this reason.
        .concurrent_updates(32)
        # Queues outgoing calls so a burst of users cannot trip Telegram's
        # flood limits and get the bot temporarily blocked.
        .rate_limiter(AIORateLimiter())
        .post_init(_on_startup)
        .post_shutdown(_on_shutdown)
    )
    if settings.telegram_proxy:
        # Both, deliberately: PTB keeps a second connection pool for long
        # polling, and routing only the first would leave the bot able to
        # answer and unable to hear.
        builder = builder.proxy(settings.telegram_proxy).get_updates_proxy(
            settings.telegram_proxy
        )
        logger.info("Talking to Telegram through %s", settings.telegram_proxy)

    application = builder.build()
    application.bot_data["api_client"] = client
    application.bot_data["bot"] = bot
    application.bot_data["store"] = store
    application.bot_data["settings"] = settings

    register_handlers(application, bot)
    return application


def run() -> None:
    configure_logging()
    settings = load_settings()
    add_file_logging(settings)
    ensure_ffmpeg_available()
    sweep_work_dir(settings)

    application = build_application(settings)
    application.run_polling(
        allowed_updates=Update.ALL_TYPES, drop_pending_updates=True
    )


__all__ = ["BOT_COMMANDS", "Screen", "build_application", "run"]

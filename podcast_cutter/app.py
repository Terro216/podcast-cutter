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
from .audio import ensure_ffmpeg_available
from .config import Settings, load_settings
from .handlers import PodcastCutterBot
from .states import Screen
from .store import Store

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
    ("help", "How this works"),
]


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
    application.add_handler(CommandHandler("cancel", bot.cmd_cancel))

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


def build_application(settings: Settings, store: Store | None = None) -> Application:
    client = PodcastIndexClient(settings)
    if store is None:
        store = Store(settings.database_path)
        store.connect()
    bot = PodcastCutterBot(settings, client, store)

    application = (
        ApplicationBuilder()
        .token(settings.bot_token)
        # Queues outgoing calls so a burst of users cannot trip Telegram's
        # flood limits and get the bot temporarily blocked.
        .rate_limiter(AIORateLimiter())
        .post_init(_on_startup)
        .post_shutdown(_on_shutdown)
        .build()
    )
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

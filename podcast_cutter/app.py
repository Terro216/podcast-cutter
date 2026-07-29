"""Application wiring: handler registration and lifecycle.

Registration is flat on purpose. One callback router, one text router and one
inline handler cover every interaction, so there is no ordering subtlety about
which handler shadows which — the routers themselves decide.
"""

from __future__ import annotations

import logging

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


def configure_logging() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        level=logging.INFO,
    )
    # httpx logs a line per request, including every getUpdates poll.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram.ext.Application").setLevel(logging.INFO)


async def _on_startup(application: Application) -> None:
    bot: PodcastCutterBot = application.bot_data["bot"]

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


def build_application(settings: Settings) -> Application:
    client = PodcastIndexClient(settings)
    bot = PodcastCutterBot(settings, client)

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

    register_handlers(application, bot)
    return application


def run() -> None:
    configure_logging()
    settings = load_settings()
    ensure_ffmpeg_available()

    application = build_application(settings)
    application.run_polling(
        allowed_updates=Update.ALL_TYPES, drop_pending_updates=True
    )


__all__ = ["BOT_COMMANDS", "Screen", "build_application", "run"]

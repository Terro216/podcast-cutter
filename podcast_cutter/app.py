"""Application wiring: handler registration and lifecycle."""

from __future__ import annotations

import logging
import warnings

from telegram import Update
from telegram.ext import (
    AIORateLimiter,
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telegram.warnings import PTBUserWarning

from . import keyboards as kb
from .api import PodcastIndexClient
from .audio import ensure_ffmpeg_available
from .config import Settings, load_settings
from .handlers import PodcastCutterBot
from .states import State

logger = logging.getLogger(__name__)


def configure_logging() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        level=logging.INFO,
    )
    # httpx logs a line per request, including every getUpdates poll.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram.ext.Application").setLevel(logging.INFO)


def build_conversation(
    bot: PodcastCutterBot, settings: Settings
) -> ConversationHandler:
    text_only = filters.TEXT & ~filters.COMMAND
    # Menu button presses arrive as ordinary text, so exclude them from the
    # "user typed a search term" handlers; otherwise pressing "🔥 Trending"
    # inside a search would be treated as a podcast name.
    not_menu = ~filters.Regex(kb.menu_regex(*kb.MENU_BUTTONS))
    free_text = text_only & not_menu

    entry_points = [
        CommandHandler("search", bot.ask_podcast_name),
        CommandHandler("cut_podcast", bot.ask_podcast_name),  # legacy name
        CommandHandler("person", bot.ask_person_query),
        CommandHandler("search_episodes", bot.ask_person_query),  # legacy name
        CommandHandler("trending", bot.trending),
        CommandHandler("surprise", bot.surprise),
        MessageHandler(
            filters.Regex(kb.menu_regex(kb.BTN_SEARCH_PODCAST)), bot.ask_podcast_name
        ),
        MessageHandler(
            filters.Regex(kb.menu_regex(kb.BTN_SEARCH_PERSON)), bot.ask_person_query
        ),
        MessageHandler(filters.Regex(kb.menu_regex(kb.BTN_TRENDING)), bot.trending),
        MessageHandler(filters.Regex(kb.menu_regex(kb.BTN_SURPRISE)), bot.surprise),
    ]

    # per_message=True would require every handler in the conversation to be a
    # CallbackQueryHandler, but this flow is deliberately mixed: users can type
    # a search term or tap a button at most steps. Stale button presses are
    # handled explicitly instead (see PodcastCutterBot._stale_button), so the
    # warning describes a setup we are not using.
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore", message=".*per_message=False.*", category=PTBUserWarning
        )
        return _build_conversation_handler(bot, settings, entry_points, free_text)


def _build_conversation_handler(
    bot: PodcastCutterBot, settings: Settings, entry_points: list, free_text
) -> ConversationHandler:
    return ConversationHandler(
        entry_points=entry_points,
        states={
            State.ASK_PODCAST_NAME: [
                MessageHandler(free_text, bot.handle_podcast_name),
            ],
            State.CHOOSE_PODCAST: [
                CallbackQueryHandler(bot.handle_podcast_choice),
                MessageHandler(free_text, bot.handle_podcast_name),
            ],
            State.CHOOSE_EPISODE: [
                CallbackQueryHandler(bot.handle_episode_choice),
                MessageHandler(free_text, bot.handle_episode_text),
            ],
            State.ASK_PERSON_QUERY: [
                MessageHandler(free_text, bot.handle_person_query),
            ],
            State.CHOOSE_GLOBAL_EPISODE: [
                CallbackQueryHandler(bot.handle_global_episode_choice),
                MessageHandler(free_text, bot.handle_person_query),
            ],
            State.ASK_INTERVAL: [
                MessageHandler(free_text, bot.handle_interval),
            ],
            ConversationHandler.TIMEOUT: [
                MessageHandler(filters.ALL, bot.on_timeout),
                CallbackQueryHandler(bot.on_timeout),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", bot.cancel),
            CommandHandler("start", bot.start),
            CommandHandler("help", bot.help_command),
            MessageHandler(filters.Regex(kb.menu_regex(kb.BTN_HELP)), bot.help_command),
            # A cancel button can be pressed from any state.
            CallbackQueryHandler(bot.cancel, pattern=f"^{kb.NAV_CANCEL}$"),
        ],
        # Menu buttons restart the flow instead of being swallowed by whatever
        # state the user happens to be sitting in.
        allow_reentry=True,
        conversation_timeout=settings.conversation_timeout,
        name="podcast_cutter",
    )


async def _on_startup(application: Application) -> None:
    logger.info("Bot started")


async def _on_shutdown(application: Application) -> None:
    client: PodcastIndexClient = application.bot_data["api_client"]
    await client.aclose()
    logger.info("Bot stopped")


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

    # The conversation goes first: while a user is inside it, its own fallbacks
    # must win over the standalone /help and menu handlers below.
    application.add_handler(build_conversation(bot, settings))
    application.add_handler(CommandHandler("start", bot.start))
    application.add_handler(CommandHandler("help", bot.help_command))
    application.add_handler(
        MessageHandler(filters.Regex(kb.menu_regex(kb.BTN_HELP)), bot.help_command)
    )
    application.add_handler(
        CallbackQueryHandler(bot.cancel, pattern=f"^{kb.NAV_CANCEL}$")
    )
    application.add_handler(MessageHandler(filters.COMMAND, bot.unknown_command))

    application.add_error_handler(bot.on_error)
    return application


def run() -> None:
    configure_logging()
    settings = load_settings()
    ensure_ffmpeg_available()

    application = build_application(settings)
    application.run_polling(
        allowed_updates=Update.ALL_TYPES, drop_pending_updates=True
    )

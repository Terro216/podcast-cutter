import asyncio
import logging
import os

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    Updater,
    filters,
)

from utils.api import API
from utils.audio import cut_audio, parse_interval
from utils.constants import BOT_TOKEN

# # Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
# set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Define podcast cutting states
(
    ENTER_PODCAST_NAME,
    PODCAST_CHOICE,
    ENTER_EPISODE_NAME,
    EPISODE_CHOICE,
    CUT_INTERVAL,
    ENTER_GLOBAL_SEARCH,
    GLOBAL_EPISODE_CHOICE,
) = range(7)

api = API()
processing_lock = asyncio.Lock()


async def start_cutting(update: Update, context) -> str:
    context.user_data.clear()
    await update.message.reply_text(
        "Please enter the name of the podcast:", reply_markup=ReplyKeyboardRemove()
    )
    return ENTER_PODCAST_NAME


async def start_global_search(update: Update, context) -> str:
    context.user_data.clear()
    await update.message.reply_text(
        "Please enter a person or keyword to search across all podcast episodes:",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ENTER_GLOBAL_SEARCH


async def handle_global_search(update: Update, context):
    query = update.message.text
    context.user_data["global_search_query"] = query
    context.user_data["podcast_episode_page"] = 1
    context.user_data.pop("all_found_episodes", None)
    return await render_global_episode_page(update, context)


async def render_global_episode_page(update: Update, context):
    query = context.user_data["global_search_query"]
    page = context.user_data.get("podcast_episode_page", 1)

    try:
        if "all_found_episodes" not in context.user_data:
            context.user_data["all_found_episodes"] = await asyncio.to_thread(
                api.find_episodes_by_person, query
            )
        all_found_episodes = context.user_data["all_found_episodes"]
        ep_start = (page - 1) * api.episodes_per_page
        ep_end = ep_start + api.episodes_per_page
        cur_page_episodes = all_found_episodes[ep_start:ep_end]

        if cur_page_episodes:
            has_next_page = ep_end < len(all_found_episodes)
            keyboard = []
            for episode in cur_page_episodes:
                podcast_title = episode.get("feedTitle", "Podcast")[:20]
                ep_title = episode.get("title", "Episode")[:30]
                keyboard.append(
                    [
                        InlineKeyboardButton(
                            f"{podcast_title}: {ep_title}",
                            callback_data=str(episode["id"]),
                        )
                    ]
                )
            if page > 1:
                keyboard.append(
                    [InlineKeyboardButton("Previous Page", callback_data="prev_page")]
                )
            if has_next_page:
                keyboard.append(
                    [InlineKeyboardButton("Next Page", callback_data="next_page")]
                )
            reply_markup = InlineKeyboardMarkup(keyboard)

            text = f"Found episodes for '{query}'. Please choose one:"
            if update.message:
                await update.message.reply_text(text, reply_markup=reply_markup)
            else:
                await update.callback_query.edit_message_text(
                    text, reply_markup=reply_markup
                )
            return GLOBAL_EPISODE_CHOICE
        else:
            raise Exception("No episodes found.")
    except Exception as e:
        error_message = str(e)
        logging.error(f"Error in global search: {error_message}")
        msg = (
            f"An error occurred: {error_message}\nPlease enter a different search term:"
        )
        if update.message:
            await update.message.reply_text(msg)
        else:
            await update.callback_query.edit_message_text(msg)
        context.user_data.pop("all_found_episodes", None)
        return ENTER_GLOBAL_SEARCH


async def handle_global_episode_choice(update: Update, context):
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        if query.data == "next_page":
            context.user_data["podcast_episode_page"] = (
                context.user_data.get("podcast_episode_page", 1) + 1
            )
            return await render_global_episode_page(update, context)
        elif query.data == "prev_page":
            context.user_data["podcast_episode_page"] = (
                context.user_data.get("podcast_episode_page", 1) - 1
            )
            return await render_global_episode_page(update, context)
        else:
            episode_id = query.data
            context.user_data["episode_id"] = episode_id
            all_found_episodes = context.user_data.get("all_found_episodes", [])
            for ep in all_found_episodes:
                if str(ep.get("id")) == str(episode_id):
                    context.user_data["episode_url"] = ep.get("enclosureUrl")
                    context.user_data["episode_title"] = ep.get("title")
                    context.user_data["podcast_title"] = ep.get("feedTitle")
                    break
            await query.edit_message_text(
                f"Selected episode: {context.user_data.get('episode_title', 'Unknown')}\nPlease enter the start-end interval (e.g., 01:20-02:00):"
            )
            return CUT_INTERVAL


async def start_surprise(update: Update, context) -> int:
    context.user_data.clear()
    message = await update.message.reply_text(
        "Fetching a random episode...", reply_markup=ReplyKeyboardRemove()
    )
    try:
        random_episodes = await asyncio.to_thread(api.get_random_episode, 1)
        await message.delete()
        if random_episodes:
            episode = random_episodes[0]
            context.user_data["episode_url"] = episode.get("enclosureUrl")
            context.user_data["episode_title"] = episode.get("title", "Random Episode")

            await update.message.reply_text(
                f"Surprise! We picked: {context.user_data['episode_title']}\n\nPlease enter the interval you want to cut (e.g., 01:20-02:00):"
            )
            return CUT_INTERVAL
        else:
            await update.message.reply_text("No random episodes found.")
            return ConversationHandler.END
    except Exception as e:
        logging.error(f"Error in start_surprise: {e}")
        await message.delete()
        await update.message.reply_text(
            "An error occurred while fetching a random episode."
        )
        return ConversationHandler.END


async def start_trending(update: Update, context) -> int:
    context.user_data.clear()
    message = await update.message.reply_text(
        "Fetching trending podcasts...", reply_markup=ReplyKeyboardRemove()
    )
    try:
        trending_feeds = await asyncio.to_thread(api.get_trending_podcasts)
        await message.delete()
        if trending_feeds:
            context.user_data["found_feeds_dict"] = {
                str(feed["id"]): feed["title"] for feed in trending_feeds
            }
            keyboard = []
            for feed in trending_feeds:
                keyboard.append(
                    [
                        InlineKeyboardButton(
                            f"Title: {feed.get('title', 'Unknown')}, Author: {feed.get('author', 'Unknown')}",
                            callback_data=str(feed["id"]),
                        )
                    ]
                )
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(
                "Here are the trending podcasts:", reply_markup=reply_markup
            )
            return PODCAST_CHOICE
        else:
            await update.message.reply_text("No trending podcasts found.")
            return ConversationHandler.END
    except Exception as e:
        logging.error(f"Error in start_trending: {e}")
        await message.delete()
        await update.message.reply_text(
            "An error occurred while fetching trending podcasts."
        )
        return ConversationHandler.END


async def handle_podcast_name(update: Update, context):
    if update.message:
        podcast_name = update.message.text
        context.user_data["podcast_name"] = podcast_name
        context.user_data["podcast_page"] = 1
    else:
        podcast_name = context.user_data.get("podcast_name")

    podcast_page = context.user_data.get("podcast_page", 1)

    try:
        found_feeds, has_next_page = await asyncio.to_thread(
            api.find_podcasts_feeds, podcast_name, podcast_page
        )  # TODO: add memo

        if found_feeds:
            context.user_data["found_feeds_dict"] = {
                str(feed["id"]): feed["title"] for feed in found_feeds
            }
            if len(found_feeds) == 1:
                found_podcast = found_feeds[0]
                context.user_data["podcast_id"] = found_podcast["id"]
                context.user_data["podcast_title"] = found_podcast["title"]
                if update.callback_query:
                    await update.callback_query.edit_message_text(
                        f"Found podcast: {found_podcast['title']}"
                    )
                else:
                    await update.message.reply_text(
                        f"Found podcast: {found_podcast['title']}"
                    )
                logging.info(f"Selected single podcast ID: {found_podcast['id']}")
                return await handle_podcast_episode(update, context)
            else:
                # Create inline keyboard buttons for search results
                keyboard = []
                for feed in found_feeds:
                    keyboard.append(
                        [
                            InlineKeyboardButton(
                                f"Title: {feed['title']}, Author: {feed['author']}",
                                callback_data=str(feed["id"]),
                            )
                        ]
                    )
                if podcast_page > 1:
                    keyboard.append(
                        [
                            InlineKeyboardButton(
                                "Previous Page", callback_data="prev_page"
                            )
                        ]
                    )
                if has_next_page:
                    keyboard.append(
                        [InlineKeyboardButton("Next Page", callback_data="next_page")]
                    )
                reply_markup = InlineKeyboardMarkup(keyboard)

                if update.callback_query:
                    await update.callback_query.edit_message_text(
                        "Please choose a podcast:", reply_markup=reply_markup
                    )
                else:
                    await update.message.reply_text(
                        "Please choose a podcast:", reply_markup=reply_markup
                    )
                return PODCAST_CHOICE
        else:
            raise Exception("Unexpected error :(")
    except Exception as e:
        error_message = str(e)
        logging.error(f"Error in handle_podcast_name: {error_message}")
        await update.message.reply_text(
            f"An error occurred: {error_message}\nPlease enter a different podcast name:"
        )
        context.user_data["podcast_page"] = 1
        return ENTER_PODCAST_NAME


async def handle_podcast_choice(update: Update, context):
    query = update.callback_query
    await query.answer()

    if query.data == "next_page":
        context.user_data["podcast_page"] = context.user_data.get("podcast_page", 1) + 1
        return await handle_podcast_name(update, context)
    elif query.data == "prev_page":
        context.user_data["podcast_page"] = context.user_data.get("podcast_page", 1) - 1
        return await handle_podcast_name(update, context)
    else:
        podcast_id = query.data
        context.user_data["podcast_id"] = podcast_id
        context.user_data["podcast_title"] = context.user_data.get(
            "found_feeds_dict", {}
        ).get(podcast_id, "Podcast")
        logging.info(f"Selected podcast ID: {podcast_id}")
        await query.edit_message_text(
            f"Selected podcast: {context.user_data['podcast_title']}"
        )
        return await handle_podcast_episode(update, context)


async def handle_podcast_episode(update: Update, context):
    podcast_id = context.user_data["podcast_id"]
    podcast_episode_page = context.user_data.get("podcast_episode_page", 1)

    try:
        if "all_found_episodes" not in context.user_data:
            context.user_data["all_found_episodes"] = await asyncio.to_thread(
                api.find_podcast_episodes, podcast_id
            )
        all_found_episodes = context.user_data["all_found_episodes"]
        ep_start = (podcast_episode_page - 1) * api.episodes_per_page
        ep_end = ep_start + api.episodes_per_page
        cur_page_episodes = all_found_episodes[ep_start:ep_end]
        if cur_page_episodes:
            has_next_page = ep_end < len(all_found_episodes)
            keyboard = []
            for episode in cur_page_episodes:
                keyboard.append(
                    [
                        InlineKeyboardButton(
                            f"ep {episode['title']}",
                            callback_data=episode["id"],
                        )
                    ]
                )
            if podcast_episode_page > 1:
                keyboard.append(
                    [InlineKeyboardButton("Previous Page", callback_data="prev_page")]
                )
            if has_next_page:
                keyboard.append(
                    [InlineKeyboardButton("Next Page", callback_data="next_page")]
                )
            reply_markup = InlineKeyboardMarkup(keyboard)

            if update.message:
                await update.message.reply_text(
                    "Please choose episode (Or write title of the episode):",
                    reply_markup=reply_markup,
                )
            else:
                await update.callback_query.edit_message_text(
                    "Please choose episode (Or write title of the episode):",
                    reply_markup=reply_markup,
                )

            return EPISODE_CHOICE
        else:
            raise Exception("Unexpected error :(")
    except Exception as e:
        error_message = str(e)
        logging.error(f"Error in handle_podcast_episode: {error_message}")
        if update.message:
            await update.message.reply_text(
                f"An error occurred: {error_message}\nPlease enter/chose a different podcast episode:"
            )
        else:
            await update.callback_query.edit_message_text(
                f"An error occurred: {error_message}\nPlease enter/chose a different podcast episode:"
            )
        return ENTER_EPISODE_NAME


async def handle_episode_choice(update: Update, context):
    episode_name: str = "undefined.. for now"

    if update.callback_query:
        # Сообщение пришло из кнопки (CallbackQuery)
        query = update.callback_query
        await query.answer()
        print(query.data)
        if query.data == "next_page":
            context.user_data["podcast_episode_page"] = (
                context.user_data.get("podcast_episode_page", 1) + 1
            )
            return await handle_podcast_episode(update, context)
        elif query.data == "prev_page":
            context.user_data["podcast_episode_page"] = (
                context.user_data.get("podcast_episode_page", 1) - 1
            )
            return await handle_podcast_episode(update, context)
        else:
            episode_id = query.data
            context.user_data["episode_id"] = episode_id
            all_found_episodes = context.user_data.get("all_found_episodes", [])
            for ep in all_found_episodes:
                if str(ep.get("id")) == str(episode_id):
                    context.user_data["episode_url"] = ep.get("enclosureUrl")
                    context.user_data["episode_title"] = ep.get("title")
                    break
            await query.edit_message_text(
                f"Selected episode: {context.user_data.get('episode_title', 'Unknown')}\nPlease enter the start-end interval (e.g., 01:20-02:00):"
            )
            return CUT_INTERVAL
    elif update.message:
        # Сообщение пришло текстом
        episode_query = update.message.text.lower()
        all_found_episodes = context.user_data["all_found_episodes"]

        # Search by substring
        matching_episodes = [
            ep for ep in all_found_episodes if episode_query in ep["title"].lower()
        ]

        if not matching_episodes:
            await update.message.reply_text("Episode not found( try again:")
            return EPISODE_CHOICE

        if len(matching_episodes) == 1:
            episode = matching_episodes[0]
            context.user_data["episode_id"] = episode["id"]
            context.user_data["episode_url"] = episode.get("enclosureUrl")
            context.user_data["episode_title"] = episode.get("title")
            await update.message.reply_text(f"Selected episode: {episode.get('title')}")
            await update.message.reply_text(
                "Please enter the start-end interval (e.g., 01:20-02:00):"
            )
            return CUT_INTERVAL
        else:
            # Show the first 5 matches
            keyboard = []
            for episode in matching_episodes[:5]:
                keyboard.append(
                    [
                        InlineKeyboardButton(
                            f"ep {episode['title']}",
                            callback_data=str(episode["id"]),
                        )
                    ]
                )
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(
                f"Found {len(matching_episodes)} matching episodes. Please choose one:",
                reply_markup=reply_markup,
            )
            return EPISODE_CHOICE


async def handle_interval(update: Update, context):
    interval_str = update.message.text
    episode_url = context.user_data.get("episode_url")

    if not episode_url:
        await update.message.reply_text(
            "Error: Could not find audio URL for this episode."
        )
        return ConversationHandler.END

    try:
        start_sec, end_sec = parse_interval(interval_str)
    except ValueError as e:
        await update.message.reply_text(
            f"Invalid interval format: {e}. Try again (e.g., 01:20-02:00):"
        )
        return CUT_INTERVAL

    msg = await update.message.reply_text(
        "⏳ Added to processing queue... Waiting for an available slot."
    )

    async with processing_lock:
        await msg.edit_text(
            "⏳ Cutting podcast...\n\n(Note: If the host is protected, the bot will securely download the full episode in the background first. This might take a couple of minutes!)"
        )

        try:
            cut_file = await cut_audio(episode_url, start_sec, end_sec)
            await msg.edit_text("📤 Uploading your cut audio...")

            podcast_title = context.user_data.get("podcast_title", "Podcast").replace(
                " ", "_"
            )
            episode_title = context.user_data.get("episode_title", "Episode").replace(
                " ", "_"
            )
            safe_interval = interval_str.replace(":", "-").replace(" ", "")
            filename = f"{podcast_title}-{episode_title}-{safe_interval}.mp3".replace(
                "/", "_"
            ).replace("\\", "_")

            await update.message.reply_audio(
                audio=open(cut_file, "rb"),
                filename=filename,
                title=f"{context.user_data.get('podcast_title', 'Podcast')} - {context.user_data.get('episode_title', 'Episode')} ({interval_str})",
                caption="Here's your cut podcast!",
            )
            if os.path.exists(cut_file):
                os.remove(cut_file)
            await msg.delete()
        except Exception as e:
            await msg.edit_text(f"❌ Error cutting audio:\n{e}")

    return ConversationHandler.END


async def cancel(update: Update, context):
    await update.message.reply_text(
        "Task canceled. You can always try again.", reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "🎙 *Podcast Cutter Bot*\n\n"
        "Here are the available commands:\n"
        "/start - Show the main menu\n"
        "/cut_podcast - Start searching for a podcast by name\n"
        "/search_episodes - Start searching episodes globally\n"
        "/cancel - Cancel the current operation\n"
        "/help - Show this help message"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")


async def not_implemented_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await update.message.reply_text(
        f"Sorry, {update.effective_user.first_name}, but this command not implemented yet"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [
            KeyboardButton("Search Podcast by Name"),
            KeyboardButton("Search Episodes Globally"),
        ],
        [
            KeyboardButton("Trending Podcasts"),
            KeyboardButton("Surprise Me"),
        ],
        [
            KeyboardButton("Help"),
        ],
    ]
    reply_markup = ReplyKeyboardMarkup(
        keyboard, one_time_keyboard=False, resize_keyboard=True
    )
    await update.message.reply_text(
        "Hello! tap button below to do something", reply_markup=reply_markup
    )
    return ConversationHandler.END


def main() -> None:
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(
        MessageHandler(
            filters.Regex("^(Help)$"),
            help_command,
        )
    )

    cut_podcast_handler = ConversationHandler(
        entry_points=[
            CommandHandler("cut_podcast", start_cutting),
            CommandHandler("search_episodes", start_global_search),
            MessageHandler(
                filters.Regex("^Search Podcast by Name$"),
                start_cutting,
            ),
            MessageHandler(
                filters.Regex("^Search Episodes Globally$"),
                start_global_search,
            ),
            CommandHandler("trending", start_trending),
            MessageHandler(
                filters.Regex("^Trending Podcasts$"),
                start_trending,
            ),
            CommandHandler("surprise", start_surprise),
            MessageHandler(
                filters.Regex("^Surprise Me$"),
                start_surprise,
            ),
        ],
        states={
            ENTER_PODCAST_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_podcast_name)
            ],
            PODCAST_CHOICE: [
                CallbackQueryHandler(handle_podcast_choice),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_podcast_name),
            ],
            ENTER_EPISODE_NAME: [],
            EPISODE_CHOICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_episode_choice),
                CallbackQueryHandler(handle_episode_choice),
            ],
            ENTER_GLOBAL_SEARCH: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_global_search)
            ],
            GLOBAL_EPISODE_CHOICE: [
                CallbackQueryHandler(handle_global_episode_choice),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_global_search),
            ],
            CUT_INTERVAL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_interval)
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("start", start),
            CommandHandler("help", help_command),
        ],
        # per_message=True,
    )
    app.add_handler(cut_podcast_handler)

    # Run the bot until the user presses Ctrl-C
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

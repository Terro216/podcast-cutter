import logging
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
    CommandHandler,
    ContextTypes,
    Updater,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
)
from utils.api import API
from utils.constants import BOT_TOKEN


# # Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
# set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Define podcast cutting states
ENTER_PODCAST_NAME, PODCAST_CHOICE, ENTER_EPISODE_NAME, EPISODE_CHOICE, CUT_INTERVAL = (
    range(5)
)

api = API()


async def start_cutting(update: Update, context) -> str:
    await update.message.reply_text(
        "Please enter the name of the podcast:", reply_markup=ReplyKeyboardRemove()
    )
    return ENTER_PODCAST_NAME


async def handle_podcast_name(update: Update, context):
    podcast_name = update.message.text
    podcast_page = context.user_data.get("podcast_page", 1)

    try:
        found_feeds, has_next_page = api.find_podcasts_feeds(
            podcast_name, podcast_page
        )  # TODO: add memo

        if found_feeds:
            if len(found_feeds) == 1:
                found_podcast = found_feeds[0]
                context.user_data["podcast_id"] = found_podcast["id"]
                await update.message.reply_text(
                    f"Found podcast: {found_podcast['title']}"
                )
                logging.info(f"Selected single podcast ID: {found_podcast['id']}")
                return ENTER_EPISODE_NAME
            else:
                # Create inline keyboard buttons for search results
                keyboard = []
                for feed in found_feeds:
                    keyboard.append(
                        [
                            InlineKeyboardButton(
                                f'Title: {feed["title"]}, Author: {feed["author"]}',
                                callback_data=feed["id"],
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
        return ENTER_PODCAST_NAME
    elif query.data == "prev_page":
        context.user_data["podcast_page"] = context.user_data.get("podcast_page", 1) - 1
        return ENTER_PODCAST_NAME
    else:
        podcast_id = query.data
        context.user_data["podcast_id"] = podcast_id
        logging.info(f"Selected podcast ID: {podcast_id}")
        await query.edit_message_text(f"Selected podcast ID: {podcast_id}")
        return ENTER_EPISODE_NAME


async def handle_podcast_episode(update: Update, context):
    print("all_found_episodesasdadsads")  ## TODO: STOP HERE - no prints
    podcast_id = context.user_data["podcast_id"]
    podcast_episode_page = context.user_data.get("podcast_episode_page", 1)

    try:
        print("all_found_episodes")
        all_found_episodes = context.user_data.get(
            "all_found_episodes", api.find_podcast_episodes(podcast_id)
        )
        print(all_found_episodes)
        ep_end = len(all_found_episodes) / api.episodes_per_page * podcast_episode_page
        cur_page_episodes = all_found_episodes[ep_end - api.episodes_per_page : ep_end]
        if cur_page_episodes:
            has_next_page = ep_end < len(all_found_episodes)
            keyboard = []
            for episode in cur_page_episodes:
                keyboard.append(
                    [
                        InlineKeyboardButton(
                            f'ep {episode["title"]}',
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

            await update.message.reply_text(
                "Please choose episode (Or write title of the episode):",
                reply_markup=reply_markup,
            )

            return PODCAST_CHOICE
        else:
            raise Exception("Unexpected error :(")
    except Exception as e:
        error_message = str(e)
        logging.error(f"Error in handle_podcast_episode: {error_message}")
        await update.message.reply_text(
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
            return ENTER_EPISODE_NAME
        elif query.data == "prev_page":
            context.user_data["podcast_episode_page"] = (
                context.user_data.get("podcast_episode_page", 1) - 1
            )
            return ENTER_EPISODE_NAME
        else:
            episode_id = query.data
            context.user_data["episode_id"] = episode_id
            # found_episode_link = episode["enclosureUrl"]
            return CUT_INTERVAL
    elif update.message:
        # Сообщение пришло текстом
        episode_name = update.message.text
        # Сохраните выбранный эпизод в контексте пользователя
        all_found_episodes = context.user_data["all_found_episodes"]
        episode_id = None
        for episode in all_found_episodes:
            if episode["title"].lower() == episode_name.lower():
                episode_id = episode["title"]
                # found_episode_link = episode["enclosureUrl"]
                break
        if not episode_id:
            await update.message.reply_text(f"episode not found( try again")
            return ENTER_EPISODE_NAME
        context.user_data["episode_id"] = episode_id
        await update.message.reply_text(f"Ep: {episode_name}")
        await update.message.reply_text(
            "Please enter the start-end interval (in seconds):"
        )
        return CUT_INTERVAL


def handle_interval(update: Update, context):
    # TODO: todo
    end_time = update.message.text
    podcast_id = context.user_data["podcast_id"]
    start_time = context.user_data["start_time"]

    # Generate the cut podcast file based on podcast_id, start_time, and end_time
    cut_podcast_file = "file"  # generate_cut_podcast(podcast_id, start_time, end_time)

    update.message.reply_document(document=cut_podcast_file)
    update.message.reply_text("Here's your cut podcast!")

    return ConversationHandler.END


async def cancel(update: Update, context):
    await update.message.reply_text("Task canceled. You can always try again.")
    return ConversationHandler.END


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"sending help to {update.effective_user.first_name}"
    )


async def not_implemented_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await update.message.reply_text(
        f"Sorry, {update.effective_user.first_name}, but this command not implemented yet"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [
            KeyboardButton("Cut the podcast!"),
            KeyboardButton("Uncut the podcast"),
        ],
        [
            KeyboardButton("More buttons soon"),
            KeyboardButton("About"),
        ],
    ]
    reply_markup = ReplyKeyboardMarkup(
        keyboard, one_time_keyboard=False, resize_keyboard=True
    )
    await update.message.reply_text(
        "Hello! tap button below to do something", reply_markup=reply_markup
    )


def main() -> None:
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(
        MessageHandler(
            filters.Regex("^(Uncut the podcast|More buttons soon|About)$"),
            not_implemented_command,
        )
    )

    cut_podcast_handler = ConversationHandler(
        entry_points=[
            CommandHandler("cut_podcast", start_cutting),
            MessageHandler(
                filters.Regex("^Cut the podcast!$"),
                start_cutting,
            ),
        ],
        states={
            ENTER_PODCAST_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_podcast_name)
            ],
            PODCAST_CHOICE: [CallbackQueryHandler(handle_podcast_choice)],
            ENTER_EPISODE_NAME: [],
            EPISODE_CHOICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_episode_choice),
                CallbackQueryHandler(handle_episode_choice),
            ],
            CUT_INTERVAL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_interval)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        # per_message=True,
    )
    app.add_handler(cut_podcast_handler)

    # Run the bot until the user presses Ctrl-C
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

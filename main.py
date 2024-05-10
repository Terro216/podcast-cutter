import logging
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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
PODCAST_NAME, PODCAST_CHOICE, CUT_INTERVAL = range(3)

api = API()


async def start_cutting(update: Update, context) -> str:
    await update.message.reply_text("Please enter the name of the podcast:")
    return PODCAST_NAME


async def handle_podcast_name(update: Update, context):
    podcast_name = update.message.text
    # Perform search based on podcast_name and generate search results
    try:
        found_feeds = api.find_podcasts_feeds(podcast_name)

        if found_feeds:
            if len(found_feeds) == 1:
                ##! STOPPED THERE: add skip keyboard if there is only one feed found - how to move to the next stage and send callback_data
                await update.message.reply_text(
                    f"Found podcast: {found_feeds[0]['title']}",
                    # callback_data=found_feeds[0]["id"],
                )
            else:
                # Create inline keyboard buttons for search results
                keyboard = []
                for result in found_feeds:
                    keyboard.append(
                        [
                            InlineKeyboardButton(
                                f'Title: {result["title"]}, Author: {result["author"]}',
                                callback_data=result["id"],
                            )
                        ]
                    )
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
        return PODCAST_NAME


def handle_podcast_choice(update: Update, context):
    query = update.callback_query
    query.answer()

    if query.data == "next_page":
        # Handle next page logic
        # ...
        return PODCAST_CHOICE
    else:
        podcast_id = query.data
        context.user_data["podcast_id"] = podcast_id
        query.edit_message_text("Please enter the start time (in seconds):")
        return CUT_INTERVAL


def handle_interval(update: Update, context):
    end_time = update.message.text
    podcast_id = context.user_data["podcast_id"]
    start_time = context.user_data["start_time"]

    # Generate the cut podcast file based on podcast_id, start_time, and end_time
    cut_podcast_file = "file"  # generate_cut_podcast(podcast_id, start_time, end_time)

    update.message.reply_document(document=cut_podcast_file)
    update.message.reply_text("Here's your cut podcast!")

    return ConversationHandler.END


async def cancel(update: Update, context):
    await update.message.reply_text("Task canceled. You can always try again")
    return ConversationHandler.END


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f"help to {update.effective_user.first_name}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f"Hello! write /cut_podcast")


def main() -> None:
    """Start the bot."""

    # Create the Application and pass it your bot's token.
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # on different commands - answer in Telegram
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("start", start))

    # on non command i.e message - echo the message on Telegram
    # app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

    cut_podcast_handler = ConversationHandler(
        entry_points=[CommandHandler("cut_podcast", start_cutting)],
        states={
            PODCAST_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_podcast_name)
            ],
            PODCAST_CHOICE: [CallbackQueryHandler(handle_podcast_choice)],
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

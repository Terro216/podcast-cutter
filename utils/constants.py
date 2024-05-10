import os
from dotenv import load_dotenv

load_dotenv()

PODCAST_API_BASEURL = os.getenv("PODCAST_API_BASEURL")
PODCAST_API_KEY = os.getenv("PODCAST_API_KEY")
PODCAST_API_SECRET = os.getenv("PODCAST_API_SECRET")
BOT_TOKEN = os.getenv("BOT_TOKEN")

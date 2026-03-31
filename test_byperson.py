import hashlib
import json
import os
import time

import requests
from dotenv import load_dotenv

load_dotenv()

PODCAST_API_BASEURL = os.getenv(
    "PODCAST_API_BASEURL", "https://api.podcastindex.org/api/1.0"
)
PODCAST_API_KEY = os.getenv("PODCAST_API_KEY")
PODCAST_API_SECRET = os.getenv("PODCAST_API_SECRET")


def get_headers():
    header_time = str(int(time.time()))
    return {
        "X-Auth-Key": PODCAST_API_KEY,
        "User-Agent": "PodcastCutter/1.0",
        "X-Auth-Date": header_time,
        "Authorization": hashlib.sha1(
            (PODCAST_API_KEY + PODCAST_API_SECRET + header_time).encode()
        ).hexdigest(),
    }


def test_byperson(query):
    print(f"Searching for episodes by person/query: '{query}'")
    params = {
        "q": query,
        "pretty": True,
    }
    response = requests.get(
        f"{PODCAST_API_BASEURL}/search/byperson",
        params=params,
        headers=get_headers(),
    )

    if response.status_code == 200:
        data = response.json()
        print(f"Found {data.get('count', 0)} episodes.")
        print("Showing first 3 results:\n")

        for item in data.get("items", [])[:3]:
            print(f"🎙 Podcast: {item.get('feedTitle')}")
            print(f"▶️ Episode: {item.get('title')}")
            print(f"🔗 URL: {item.get('enclosureUrl')}\n")
    else:
        print(f"Error {response.status_code}: {response.text}")


if __name__ == "__main__":
    if not PODCAST_API_KEY or not PODCAST_API_SECRET:
        print("Error: Missing PODCAST_API_KEY or PODCAST_API_SECRET in .env")
    else:
        test_byperson("Lex Fridman")

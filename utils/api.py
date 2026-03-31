import hashlib
import json
import time
from typing import List

import requests

from utils.constants import (
    PODCAST_API_BASEURL,
    PODCAST_API_KEY,
    PODCAST_API_SECRET,
)

# type = ["bytitle", "byterm", "byperson"]


class API:
    def new(cls):
        if not hasattr(cls, "instance"):
            cls.instance = super(API, cls).new(cls)
        return cls.instance

    def __init__(self):
        self.api_key = PODCAST_API_KEY
        self.api_secret = PODCAST_API_SECRET
        self.base_url = PODCAST_API_BASEURL
        self.podcasts_per_page = 5
        self.episodes_per_page = 5

    def _get_headers(self):
        header_time = str(int(time.time()))
        return {
            "X-Auth-Key": self.api_key,
            "User-Agent": "PodcastCutter/1.0",
            "X-Auth-Date": header_time,
            "Authorization": hashlib.sha1(
                (self.api_key + self.api_secret + header_time).encode()
            ).hexdigest(),
        }

    def find_podcasts_feeds(
        self, podcast_name: str, podcast_page: int = 1, type: str = "byterm"
    ) -> List:
        params = {
            "q": podcast_name,
            "pretty": True,
            "max": self.podcasts_per_page * podcast_page + 1,
        }
        response = requests.get(
            self.base_url + f"/search/{type}",
            params=params,
            headers=self._get_headers(),
        )

        if response.status_code == 200:
            data = response.json()
            # print(json.dumps(data, indent=2))
            if data["count"] > 0:
                podcast_feeds = data["feeds"]

                # Find the podcast feed with the freshest lastUpdateTime
                # freshest_feed = podcast_feeds[0]
                # freshest_timestamp = int(podcast_feeds[0]["lastUpdateTime"])

                # for feed in podcast_feeds:
                #     if "lastUpdateTime" in feed:
                #         timestamp = int(feed["lastUpdateTime"])
                #         if timestamp > freshest_timestamp:
                #             freshest_timestamp = timestamp
                #             freshest_feed = feed

                # freshest_feed_id = freshest_feed["id"]

                return [
                    podcast_feeds[-self.podcasts_per_page :],
                    data["count"] > self.podcasts_per_page * podcast_page,
                ]
            else:
                raise Exception("Podcast not found")
        else:
            raise Exception(response.status_code)

    def find_podcast_episodes(
        self, podcast_id: str, direction: str = "reverse_chrono", type: str = "byfeedid"
    ):
        params = {
            "id": podcast_id,
            "pretty": True,
        }
        response = requests.get(
            self.base_url + f"/episodes/{type}",
            params=params,
            headers=self._get_headers(),
        )

        if response.status_code == 200:
            data = response.json()
            if data["count"] > 0:
                return data["items"]
            else:
                raise Exception("Episodes not found")
        else:
            raise Exception(response.status_code)

    def find_episodes_by_person(self, person_name: str):
        """
        Searches all episodes across podcasts where the specified person is mentioned
        in tags, title, description, or author fields.
        """
        params = {
            "q": person_name,
            "pretty": True,
        }
        response = requests.get(
            self.base_url + "/search/byperson",
            params=params,
            headers=self._get_headers(),
        )

        if response.status_code == 200:
            data = response.json()
            if data.get("count", 0) > 0:
                return data["items"]
            else:
                raise Exception("No episodes found matching that person/keyword")
        else:
            raise Exception(f"API Error {response.status_code}: {response.text}")

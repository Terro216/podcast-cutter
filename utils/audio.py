import asyncio
import logging
import os
import re
import subprocess
import uuid
from typing import Tuple

import requests

logger = logging.getLogger(__name__)


def parse_time_to_seconds(time_str: str) -> int:
    """
    Parses a time string into seconds.
    Supports formats:
    - MM:SS
    - HH:MM:SS
    - 1m30s
    - 90 (raw seconds)
    """
    time_str = time_str.strip().lower()

    # Raw seconds
    if time_str.isdigit():
        return int(time_str)

    # Xh Ym Zs format
    match = re.match(r"(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+)s)?", time_str)
    if match and any(match.groups()):
        h, m, s = match.groups()
        hours = int(h) if h else 0
        minutes = int(m) if m else 0
        seconds = int(s) if s else 0
        return hours * 3600 + minutes * 60 + seconds

    # HH:MM:SS or MM:SS format
    parts = time_str.split(":")
    if len(parts) == 2:
        return int(parts[0]) * 60 + int(parts[1])
    elif len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])

    raise ValueError(f"Invalid time format: {time_str}")


def parse_interval(interval_str: str) -> Tuple[int, int]:
    """
    Parses an interval string like '01:20-02:00' into start and end seconds.
    """
    if "-" not in interval_str:
        raise ValueError("Interval must contain a hyphen (e.g., 01:20-02:00)")

    start_str, end_str = interval_str.split("-", 1)
    start_sec = parse_time_to_seconds(start_str)
    end_sec = parse_time_to_seconds(end_str)

    if start_sec >= end_sec:
        raise ValueError("Start time must be before end time")

    return start_sec, end_sec


async def cut_audio(
    audio_url: str, start_sec: int, end_sec: int, output_ext: str = "mp3"
) -> str:
    """
    Cuts an audio segment from a URL using FFmpeg without downloading the entire file.
    Returns the path to the downloaded temporary file.
    """
    duration = end_sec - start_sec

    # Limits for safety (e.g., max 15 minutes)
    if duration > 15 * 60:
        raise ValueError(
            "Cut duration cannot exceed 15 minutes to respect Telegram limits."
        )

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Accept": "*/*",
        "Accept-Encoding": "identity",
    }
    loop = asyncio.get_event_loop()

    # Resolve URL redirects in Python first
    try:
        response = await loop.run_in_executor(
            None,
            lambda: requests.get(
                audio_url,
                headers=headers,
                stream=True,
                allow_redirects=True,
                timeout=15,
            ),
        )
        resolved_url = response.url
        response.close()
    except Exception as e:
        logger.warning(f"Failed to resolve redirect for {audio_url}: {e}")
        resolved_url = audio_url

    output_filename = f"/tmp/podcast_cut_{uuid.uuid4().hex[:8]}.{output_ext}"

    # -ss before -i seeks fast. -t defines duration. -c copy avoids re-encoding.
    cmd = [
        "ffmpeg",
        "-y",  # Overwrite
        "-user_agent",
        headers["User-Agent"],
        "-ss",
        str(start_sec),  # Start time
        "-i",
        resolved_url,  # Input URL
        "-t",
        str(duration),  # Duration
        "-c",
        "copy",  # Copy codec directly (fast)
        "-map",
        "0:a",  # Map only audio (avoid cover image issues)
        output_filename,
    ]

    logger.info(f"Running FFmpeg (stream): {' '.join(cmd)}")

    process = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()

    if process.returncode != 0:
        logger.warning(
            f"FFmpeg stream failed, attempting full download: {stderr.decode()}"
        )
        temp_input_filename = f"/tmp/podcast_full_{uuid.uuid4().hex[:8]}.tmp"
        try:

            def _download():
                # Some servers require range requests or block requests without referer
                dl_headers = headers.copy()
                dl_headers["Referer"] = "https://google.com/"

                with requests.get(
                    resolved_url, headers=dl_headers, stream=True, timeout=30
                ) as r:
                    # Specific error message for 403 blocks
                    if r.status_code == 403:
                        raise Exception(
                            "The podcast server (Anchor/Spotify) is actively blocking downloads from this server's IP address (geofence)."
                        )
                    r.raise_for_status()
                    with open(temp_input_filename, "wb") as f:
                        for chunk in r.iter_content(chunk_size=8192):
                            if chunk:
                                f.write(chunk)

            logger.info(f"Downloading {resolved_url} to {temp_input_filename}...")
            await loop.run_in_executor(None, _download)

            cmd[cmd.index(resolved_url)] = temp_input_filename
            logger.info(f"Running FFmpeg (local): {' '.join(cmd)}")

            process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()

            if process.returncode != 0:
                logger.error(f"FFmpeg local file error: {stderr.decode()}")
                raise Exception("Failed to cut audio even after downloading.")

        except Exception as e:
            if os.path.exists(output_filename):
                os.remove(output_filename)
            raise Exception(f"Failed to cut audio stream: {e}")
        finally:
            if os.path.exists(temp_input_filename):
                os.remove(temp_input_filename)

    return output_filename

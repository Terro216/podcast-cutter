# Podcast Cutter Bot - Project Overview & Development Plan

## Project Overview
`podcast-cutter` is a Telegram bot built with `python-telegram-bot` that allows users to search for podcasts, browse episodes, and specify a time interval to extract a specific segment of the audio. The extracted segment is then sent back to the user as an audio file, ready to be shared.

Currently, the bot implements a basic conversation handler with states for searching podcasts and episodes via an external API. However, the actual audio downloading, cutting mechanism, and robust input handling are missing or incomplete. 

## Development Plan: Path to Production-Ready

To make this bot "unbreakable" and production-ready, we need to implement the core logic, fix existing bugs, enforce limits, and add polish. 

### 1. Core Functionality Implementation
- **Audio Processing Engine**: Integrate `ffmpeg` (via `ffmpeg-python` or `pydub`) to handle downloading and cutting audio streams. Whenever possible, we should stream the cut directly or download only the necessary chunks to save bandwidth and storage.
- **Interval Parsing**: Create a robust parser for the `CUT_INTERVAL` state. It must support multiple formats (e.g., `MM:SS-MM:SS`, `HH:MM:SS-HH:MM:SS`, or `1m30s-2m`) and validate the input against the actual episode length.
- **Conversation Flow Fixes**: 
  - Fix synchronous/asynchronous mismatches in `main.py` (e.g., `handle_interval` is currently synchronous).
  - Fix the `ENTER_EPISODE_NAME` and `EPISODE_CHOICE` states which currently have mixed logic.

### 2. Limits and Safeguards (Making it Unbreakable)
- **Duration/Size Limits**: Telegram bots have a standard 50MB file upload limit. We must enforce a maximum cut duration (e.g., 10-15 minutes max, depending on bitrate) to guarantee the file can be sent.
- **Asynchronous Processing**: Audio processing can be slow. Downloading and cutting must be offloaded to background tasks (`asyncio.create_task` or a task queue like Celery/Redis) so the bot doesn't freeze for other users.
- **Disk Space Management**: Implement a strict cleanup mechanism. Temporary audio files must be deleted immediately after sending or if an error occurs.
- **Graceful Error Handling**: Catch API timeouts, invalid RSS feeds, unavailable audio files, and parsing errors. The user should always receive a friendly error message and be returned to a safe state.
- **Rate Limiting & Throttling**: Prevent users from spamming heavy processing requests. Limit users to 1 concurrent cutting task.

### 3. Fancy Features & Polish
- **Live Progress Updates**: Since downloading and cutting takes time, send a "Processing..." message and update it with a progress bar (e.g., `[██████░░░░] 60% Downloading...`).
- **Rich Media Metadata (ID3 Tags)**: Apply proper ID3 tags to the cut audio file. Set the title to `[Cut] Original Title`, author to the podcast author, and attach the podcast's cover art as the album thumbnail.
- **Inline Keyboard Improvements**: Add "Cancel" buttons at every step of the conversation. Improve pagination UI.
- **Caching**: Cache podcast search results and episode lists (using an in-memory store or Redis) to reduce external API calls and speed up navigation.
- **Welcome / About / Help**: Polish the `/start`, `/help`, and "About" sections with clear instructions on how to use the bot.

## Next Steps for Implementation
1. Review and refactor `main.py` to fix the conversation handler states and callback queries.
2. Implement the `utils.audio` module for parsing timestamps and wrapping `ffmpeg`.
3. Add downloading and processing logic inside `handle_interval`.
4. Apply the file cleanup context managers and Telegram file upload logic.
5. Add metadata tagging and progress updating.
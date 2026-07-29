FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    POETRY_VERSION=1.8.3 \
    POETRY_VIRTUALENVS_CREATE=false \
    POETRY_NO_INTERACTION=1 \
    POETRY_HOME="/opt/poetry" \
    PATH="/opt/poetry/bin:$PATH"

# ffmpeg ships ffprobe too; both are required at runtime. sqlite3 is not used
# by the bot — it is there so the journal can be queried without copying the
# database off the host.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

RUN curl -sSL https://install.python-poetry.org | python3 - \
    && rm -rf /root/.cache

WORKDIR /app

# Dependencies first, so editing source does not invalidate this layer.
# poetry.lock is not optional: without it the build silently resolves fresh
# versions and stops matching what was tested.
COPY pyproject.toml poetry.lock ./
# --only main keeps pytest and ruff out of the runtime image.
RUN poetry install --only main --no-root --no-ansi \
    && rm -rf /root/.cache

# Copy just what the bot runs, rather than the whole context.
COPY podcast_cutter/ ./podcast_cutter/
COPY main.py ./

# Scratch space for in-flight cuts, and the mount point for durable state.
# /data must exist and be owned by `bot` in the image: a named volume inherits
# the ownership of its mount point when it is first created, and without this
# the unprivileged user cannot write to it.
ENV WORK_DIR=/var/tmp/podcast-cutter \
    DATA_DIR=/data
RUN useradd --create-home --uid 10001 bot \
    && mkdir -p "$WORK_DIR" "$DATA_DIR/logs" \
    && chown -R bot:bot "$WORK_DIR" "$DATA_DIR" /app
USER bot

VOLUME ["/data"]

CMD ["python", "main.py"]

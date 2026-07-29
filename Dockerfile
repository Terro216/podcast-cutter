FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    POETRY_VERSION=1.8.3 \
    POETRY_VIRTUALENVS_CREATE=false \
    POETRY_NO_INTERACTION=1 \
    POETRY_HOME="/opt/poetry" \
    PATH="/opt/poetry/bin:$PATH"

# ffmpeg ships ffprobe too; both are required at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
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

# Scratch space for in-flight cuts, writable by the unprivileged user below.
ENV WORK_DIR=/var/tmp/podcast-cutter
RUN useradd --create-home --uid 10001 bot \
    && mkdir -p "$WORK_DIR" \
    && chown -R bot:bot "$WORK_DIR" /app
USER bot

CMD ["python", "main.py"]

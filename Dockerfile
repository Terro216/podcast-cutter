# Use official Python slim image as base
FROM python:3.12-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    POETRY_VERSION=1.8.2 \
    POETRY_VIRTUALENVS_CREATE=false \
    POETRY_HOME="/opt/poetry" \
    PATH="/opt/poetry/bin:$PATH"

# Install system dependencies (ffmpeg is required for audio processing)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Poetry
RUN curl -sSL https://install.python-poetry.org | python3 -

# Set work directory
WORKDIR /app

# Copy dependency files first for layer caching
COPY pyproject.toml poetry.lock* ./

# Install project dependencies
RUN poetry install --no-root --no-interaction --no-ansi

# Copy the rest of the application
COPY . .

# Run the bot
CMD ["python", "main.py"]

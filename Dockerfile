FROM python:3.12-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Moscow \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY requirements.txt /app/requirements.txt

RUN python -m pip install --upgrade pip setuptools wheel \
    && python -m pip install -r /app/requirements.txt \
    && python -m playwright install --with-deps chromium \
    && chmod -R a+rX /ms-playwright \
    && rm -rf /var/lib/apt/lists/*

COPY . /app

RUN mkdir -p \
      /app/data/images \
      /app/data/telegram_web_profile \
      /app/data/telegram_web_debug

ENV TG_WEB_HEADLESS=true \
    TG_WEB_PROFILE_DIR=/app/data/telegram_web_profile \
    TG_WEB_DEBUG_DIR=/app/data/telegram_web_debug \
    DB_PATH=/app/data/bot.db

CMD ["python", "bot.py"]
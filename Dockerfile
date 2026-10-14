FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATABASE_PATH=/app/data/bot.sqlite3

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 bot \
    && useradd --uid 10001 --gid bot --no-create-home bot \
    && mkdir /app/data \
    && chown bot:bot /app/data

COPY khimki_bot.py settings.py storage.py bot_service.py delivery.py ./
USER bot
CMD ["python", "khimki_bot.py"]

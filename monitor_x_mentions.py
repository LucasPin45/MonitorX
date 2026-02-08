# monitor_x_mentions.py
import os
import json
import logging
import asyncio
from datetime import datetime, timezone
from pathlib import Path

import tweepy
from telegram import Bot

# =========================
# Logging
# =========================
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# =========================
# Env vars (required)
# =========================
TWITTER_BEARER_TOKEN = os.getenv("TWITTER_BEARER_TOKEN")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# =========================
# Config
# =========================
# Conta correta informada por você:
ZANATTA_HANDLE = "apropriajulia"

# Query: pega menções explícitas e citações comuns.
# Observação: remover lang:pt reduz risco de perder tweets sem tag de idioma.
QUERY = (
    f'("Julia Zanatta" OR @{ZANATTA_HANDLE} OR "{ZANATTA_HANDLE}" OR "Deputada Zanatta" OR "Dep. Zanatta" OR Zanatta) '
    "-is:retweet"
)

MAX_RESULTS = int(os.getenv("MAX_TWEETS", "20"))  # até 100 por chamada (v2)
TEXT_TRIM = int(os.getenv("TEXT_TRIM", "220"))    # corta texto p/ mensagem ficar legível

STATE_PATH = Path(os.getenv("STATE_PATH", ".cache/monitor_x_mentions_state.json"))

# Telegram tem limite ~4096; usamos margem.
TELEGRAM_MAX_LEN = 3900


def require_env(name: str, value: str | None) -> None:
    if not value:
        raise RuntimeError(f"Variável de ambiente ausente: {name}")


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("Falha ao ler state; recriando.")
    return {"sent_tweet_ids": []}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def search_mentions() -> list[dict]:
    """
    Busca menções recentes via X API v2.
    Retorna lista de dicts: {id, created_at, text}
    """
    require_env("TWITTER_BEARER_TOKEN", TWITTER_BEARER_TOKEN)

    client = tweepy.Client(bearer_token=TWITTER_BEARER_TOKEN, wait_on_rate_limit=True)
    resp = client.search_recent_tweets(
        query=QUERY,
        max_results=min(MAX_RESULTS, 100),
        tweet_fields=["created_at", "text"],
    )
    data = resp.data or []

    results = []
    for t in data:
        results.append(
            {
                "id": str(t.id),
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "text": t.text or "",
            }
        )
    return results


def format_datetime(iso_str: str | None) -> str:
    if not iso_str:
        return ""
    # Mantém simples e legível
    # Ex: 2026-02-08T02:44:00+00:00 -> 08/02 23:44 (local)
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%d/%m %H:%M")
    except Exception:
        return iso_str


def build_message(new_mentions: list[dict]) -> str:
    ts = datetime.now(timezone.utc).astimezone().strftime("%d/%m/%Y %H:%M")
    msg = f"📡 Monitor X — {ts}\n"
    msg += f"🗣️ Novas menções: {len(new_mentions)}\n\n"

    # Ordena por created_at (quando existir) para ficar do mais recente pro mais antigo
    def sort_key(x: dict):
        return x.get("created_at") or ""

    new_mentions_sorted = sorted(new_mentions, key=sort_key, reverse=True)

    for t in new_mentions_sorted:
        text = (t["text"] or "").replace("\n", " ").strip()
        if len(text) > TEXT_TRIM:
            text = text[:TEXT_TRIM].rstrip() + "…"

        when = format_datetime(t.get("created_at"))
        url = f"https://x.com/i/web/status/{t['id']}"

        line = f"• {when} — {text}\n  🔗 {url}\n\n"
        if len(msg) + len(line) > TELEGRAM_MAX_LEN:
            msg += "…\n"
            break
        msg += line

    return msg.strip()


async def send_telegram(text: str) -> None:
    require_env("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN)
    require_env("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID)

    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text)


def main() -> None:
    # Exige Telegram e Twitter
    require_env("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN)
    require_env("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID)
    require_env("TWITTER_BEARER_TOKEN", TWITTER_BEARER_TOKEN)

    state = load_state()
    sent_ids = set(state.get("sent_tweet_ids", []))

    try:
        mentions = search_mentions()
    except Exception as e:
        logger.exception("Falha ao buscar menções no X: %s", e)
        return

    # Filtra só o que é novo (dedupe)
    new_mentions = [m for m in mentions if m["id"] not in sent_ids]

    if not new_mentions:
        logger.info("Sem novidades: não enviando Telegram (anti-flood).")
        return

    # Atualiza state ANTES de enviar (para evitar duplicar em caso de retry)
    # Mantém uma janela para não crescer infinito.
    updated_ids = (list(sent_ids) + [m["id"] for m in new_mentions])[-400:]
    state["sent_tweet_ids"] = updated_ids
    save_state(state)

    message = build_message(new_mentions)
    asyncio.run(send_telegram(message))
    logger.info("Enviado ao Telegram: %s novas menções.", len(new_mentions))


if __name__ == "__main__":
    main()

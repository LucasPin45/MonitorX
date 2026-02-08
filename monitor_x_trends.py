import os
import json
import logging
import asyncio
from datetime import datetime, timezone
from pathlib import Path

import tweepy
from telegram import Bot

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TWITTER_API_KEY = os.getenv("TWITTER_API_KEY")
TWITTER_API_SECRET = os.getenv("TWITTER_API_SECRET")
TWITTER_ACCESS_TOKEN = os.getenv("TWITTER_ACCESS_TOKEN")
TWITTER_ACCESS_SECRET = os.getenv("TWITTER_ACCESS_SECRET")
TWITTER_BEARER_TOKEN = os.getenv("TWITTER_BEARER_TOKEN")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

BRAZIL_WOEID = 23424768

POLITICS_KEYWORDS = [
    "política", "governo", "congresso", "câmara", "senado",
    "lula", "bolsonaro", "stf", "pt", "pl", "psdb", "psol", "mdb", "pp", "união",
    "eleição", "eleições", "imposto", "reforma", "cpi", "cpmi", "pec", "plp", "mpv",
]

# ATUALIZE AQUI com o @ correto
ZANATTA_QUERY = '("Julia Zanatta" OR @apropriajulia OR "Deputada Zanatta") -is:retweet lang:pt'

MAX_TWEETS = int(os.getenv("MAX_TWEETS", "15"))
MAX_TRENDS = int(os.getenv("MAX_TRENDS", "25"))
STATE_PATH = Path(os.getenv("STATE_PATH", ".cache/monitor_x_state.json"))

TELEGRAM_MAX_LEN = 4000


def require_env(name: str, value: str | None) -> None:
    if not value:
        raise RuntimeError(f"Variável de ambiente ausente: {name}")


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("Falha ao ler state; recriando.")
    return {"last_trends_hash": None, "last_tweet_ids": []}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def short_hash(items: list[str]) -> str:
    import hashlib
    payload = "\n".join(items).encode("utf-8", errors="ignore")
    return hashlib.sha256(payload).hexdigest()[:16]


def trends_v11() -> list[str]:
    # Trends é v1.1: pode dar 403 dependendo do plano
    require_env("TWITTER_API_KEY", TWITTER_API_KEY)
    require_env("TWITTER_API_SECRET", TWITTER_API_SECRET)
    require_env("TWITTER_ACCESS_TOKEN", TWITTER_ACCESS_TOKEN)
    require_env("TWITTER_ACCESS_SECRET", TWITTER_ACCESS_SECRET)

    auth = tweepy.OAuth1UserHandler(
        TWITTER_API_KEY,
        TWITTER_API_SECRET,
        TWITTER_ACCESS_TOKEN,
        TWITTER_ACCESS_SECRET,
    )
    api = tweepy.API(auth, wait_on_rate_limit=True)
    res = api.get_place_trends(BRAZIL_WOEID)
    names = [t["name"] for t in res[0]["trends"]]
    return names[:MAX_TRENDS]


def filter_political_trends(trends: list[str]) -> list[str]:
    out = []
    for tr in trends:
        tr_low = tr.lower()
        if any(kw in tr_low for kw in POLITICS_KEYWORDS):
            out.append(tr)
    return out


def search_zanatta_v2() -> list[dict]:
    if not TWITTER_BEARER_TOKEN:
        logger.warning("TWITTER_BEARER_TOKEN não configurado; pulando busca v2.")
        return []

    client = tweepy.Client(bearer_token=TWITTER_BEARER_TOKEN, wait_on_rate_limit=True)
    resp = client.search_recent_tweets(
        query=ZANATTA_QUERY,
        max_results=min(MAX_TWEETS, 100),
        tweet_fields=["created_at", "text"],
    )
    data = resp.data or []
    out = []
    for t in data:
        out.append(
            {
                "id": str(t.id),
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "text": t.text or "",
            }
        )
    return out


def build_message(political_trends: list[str], tweets: list[dict], state: dict) -> tuple[str, dict]:
    trends_hash = short_hash(political_trends)
    trends_changed = (trends_hash != state.get("last_trends_hash"))

    seen_ids = set(state.get("last_tweet_ids", []))
    new_tweets = [t for t in tweets if t["id"] not in seen_ids]

    new_state = dict(state)
    new_state["last_trends_hash"] = trends_hash
    keep_ids = (list(seen_ids) + [t["id"] for t in new_tweets])[-200:]
    new_state["last_tweet_ids"] = keep_ids

    ts = datetime.now(timezone.utc).astimezone().strftime("%d/%m/%Y %H:%M")
    msg = f"📡 Monitor X — {ts}\n\n"

    if trends_changed and political_trends:
        msg += "🔥 Trends políticas (BR):\n"
        for tr in political_trends[:MAX_TRENDS]:
            msg += f"• {tr}\n"
    else:
        msg += "🔥 Trends políticas (BR): indisponível/sem mudança (plano pode bloquear v1.1).\n"

    msg += "\n"

    if new_tweets:
        msg += "🗣️ Menções recentes à Julia Zanatta (novas):\n"
        for t in new_tweets[:MAX_TWEETS]:
            text = (t["text"] or "").replace("\n", " ").strip()
            if len(text) > 180:
                text = text[:180].rstrip() + "…"
            url = f"https://x.com/i/web/status/{t['id']}"
            msg += f"• {text}\n  🔗 {url}\n"
    else:
        msg += "🗣️ Menções à Julia Zanatta: nada novo desde o último envio.\n"

    if len(msg) > TELEGRAM_MAX_LEN:
        msg = msg[:TELEGRAM_MAX_LEN - 1] + "…"

    return msg, new_state


async def send_telegram(message: str) -> None:
    require_env("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN)
    require_env("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID)

    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)


def main():
    state = load_state()

    political_trends = []
    try:
        trends = trends_v11()
        political_trends = filter_political_trends(trends)
    except Exception as e:
        logger.exception("Falha ao obter trends v1.1 (normal se plano bloquear): %s", e)

    tweets = []
    try:
        tweets = search_zanatta_v2()
    except Exception as e:
        logger.exception("Falha ao buscar tweets v2: %s", e)

    message, new_state = build_message(political_trends, tweets, state)

    asyncio.run(send_telegram(message))

    save_state(new_state)
    logger.info("Mensagem enviada de verdade ao Telegram.")


if __name__ == "__main__":
    main()

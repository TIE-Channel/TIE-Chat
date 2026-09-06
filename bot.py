#!/usr/bin/env python3
"""
Telegram Business auto-responder powered by Google Gemini (free tier).

Listens for messages sent to your Telegram Business account by customers and
replies automatically, in the same language the customer wrote in, keeping
per-chat conversation history.

Runs with plain long-polling: no public URL, no webhook, no framework.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import sys
import threading
import time
from collections import defaultdict, deque
from typing import Any, Deque, Dict, List, Optional

import requests

# --------------------------------------------------------------------------
# Configuration (everything comes from environment variables)
# --------------------------------------------------------------------------

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

# Model to use. Overridable; if it 404s we auto-pick a working flash model.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash").strip()

# How many previous messages (user + bot) to keep per chat.
HISTORY_TURNS = int(os.environ.get("HISTORY_TURNS", "20"))

# Minimum seconds between two auto-replies in the same chat (anti-spam).
REPLY_COOLDOWN = float(os.environ.get("REPLY_COOLDOWN", "2"))

# Max characters of a customer message we forward to the model.
MAX_INPUT_CHARS = int(os.environ.get("MAX_INPUT_CHARS", "4000"))

# Optional: comma-separated Telegram user IDs that are never auto-answered.
IGNORE_USER_IDS = {
    int(x) for x in os.environ.get("IGNORE_USER_IDS", "").replace(" ", "").split(",") if x
}

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
GEMINI_API = "https://generativelanguage.googleapis.com/v1beta"

DEFAULT_PERSONA = """\
You are the auto-reply assistant for a small business owner who is currently \
away from the keyboard.

VOICE
Write with the cadence of classic observational stand-up comedy: dry, blunt, \
irreverent, allergic to corporate filler. Short punchy sentences. A little \
world-weary. You are allowed exactly one wry aside per message, and only if it \
earns its place. This is a tone, not an impersonation of any real person, and \
you never claim to be one.

RULES
- Reply in the SAME language the customer wrote in. Match their register too: \
formal language gets formal comedy, casual gets casual.
- Be genuinely useful first, funny second. Answer the actual question.
- Keep it short: 1-3 sentences unless they asked something that needs more.
- Never insult the customer. Be sardonic about the world, never about them.
- Do not swear.
- Never invent facts about the business: no prices, no delivery dates, no \
policies, no promises. If you do not know, say the owner will confirm.
- If the message looks urgent, sensitive, or like a complaint, drop the jokes \
entirely and say the owner will get back to them personally.
- Do not use emoji. Do not use markdown headings or bullet lists.
"""

PERSONA = os.environ.get("PERSONA", DEFAULT_PERSONA)

SYSTEM_SUFFIX = """\

You are an automated assistant, not the owner. If asked directly whether you \
are a bot, say yes plainly.
"""

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("tgbiz")


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

# history[(business_connection_id, chat_id)] -> deque of {"role","text"}
history: Dict[str, Deque[Dict[str, str]]] = defaultdict(lambda: deque(maxlen=HISTORY_TURNS))

# business_connection_id -> owner's Telegram user id
owner_of_connection: Dict[str, int] = {}

# business_connection_id -> whether the bot is allowed to reply
can_reply: Dict[str, bool] = {}

last_reply_at: Dict[str, float] = {}

session = requests.Session()
session.headers["User-Agent"] = "tg-business-ai/1.0"


# --------------------------------------------------------------------------
# Telegram helpers
# --------------------------------------------------------------------------


def tg(method: str, **params: Any) -> Optional[dict]:
    """Call a Telegram Bot API method. Returns the `result` field or None."""
    try:
        r = session.post(f"{TELEGRAM_API}/{method}", json=params, timeout=70)
        data = r.json()
    except Exception as exc:  # network hiccup, bad JSON, ...
        log.warning("telegram %s failed: %s", method, exc)
        return None
    if not data.get("ok"):
        log.warning("telegram %s error: %s", method, data.get("description"))
        return None
    return data.get("result")


def resolve_owner(connection_id: str) -> Optional[int]:
    """Find the business account owner's user id for a connection."""
    if connection_id in owner_of_connection:
        return owner_of_connection[connection_id]
    res = tg("getBusinessConnection", business_connection_id=connection_id)
    if res:
        remember_connection(res)
        return owner_of_connection.get(connection_id)
    return None


def remember_connection(conn: dict) -> None:
    cid = conn.get("id")
    if not cid:
        return
    user = conn.get("user") or {}
    if user.get("id"):
        owner_of_connection[cid] = user["id"]
    # Bot API >= 9.0 exposes `rights`; older versions used `can_reply`.
    rights = conn.get("rights")
    if isinstance(rights, dict):
        can_reply[cid] = bool(rights.get("can_reply"))
    elif "can_reply" in conn:
        can_reply[cid] = bool(conn["can_reply"])
    else:
        can_reply[cid] = True
    log.info(
        "business connection %s owner=%s enabled=%s can_reply=%s",
        cid, owner_of_connection.get(cid), conn.get("is_enabled", True), can_reply.get(cid),
    )


def send_reply(connection_id: str, chat_id: int, text: str, reply_to: Optional[int]) -> None:
    # Telegram hard-limits messages to 4096 characters.
    for chunk in [text[i:i + 4000] for i in range(0, len(text), 4000)] or [text]:
        params: Dict[str, Any] = {
            "business_connection_id": connection_id,
            "chat_id": chat_id,
            "text": chunk,
        }
        if reply_to:
            params["reply_parameters"] = {"message_id": reply_to}
            reply_to = None  # only the first chunk quotes the customer
        tg("sendMessage", **params)


# --------------------------------------------------------------------------
# Gemini
# --------------------------------------------------------------------------


def pick_working_model() -> str:
    """Return GEMINI_MODEL, or fall back to the first available flash model."""
    global GEMINI_MODEL
    try:
        r = session.get(
            f"{GEMINI_API}/models",
            headers={"x-goog-api-key": GEMINI_API_KEY},
            timeout=30,
        )
        models = [
            m["name"].split("/")[-1]
            for m in r.json().get("models", [])
            if "generateContent" in m.get("supportedGenerationMethods", [])
        ]
    except Exception as exc:
        log.warning("could not list Gemini models (%s), keeping %s", exc, GEMINI_MODEL)
        return GEMINI_MODEL

    if not models:
        return GEMINI_MODEL
    if GEMINI_MODEL in models:
        log.info("using Gemini model %s", GEMINI_MODEL)
        return GEMINI_MODEL

    def rank(name: str) -> tuple:
        ver = re.search(r"(\d+(?:\.\d+)?)", name)
        return (float(ver.group(1)) if ver else 0.0, "lite" not in name)

    flash = sorted([m for m in models if "flash" in m and "preview" not in m], key=rank, reverse=True)
    chosen = flash[0] if flash else models[0]
    log.warning("model %r unavailable for this key; falling back to %r", GEMINI_MODEL, chosen)
    GEMINI_MODEL = chosen
    return chosen


def ask_gemini(key: str, user_text: str) -> Optional[str]:
    """Send the chat history plus the new message to Gemini, return the reply."""
    contents: List[dict] = [
        {"role": h["role"], "parts": [{"text": h["text"]}]} for h in history[key]
    ]
    contents.append({"role": "user", "parts": [{"text": user_text}]})

    body = {
        "system_instruction": {"parts": [{"text": PERSONA + SYSTEM_SUFFIX}]},
        "contents": contents,
        "generationConfig": {
            "temperature": float(os.environ.get("TEMPERATURE", "1.0")),
            "maxOutputTokens": int(os.environ.get("MAX_OUTPUT_TOKENS", "800")),
        },
    }

    url = f"{GEMINI_API}/models/{GEMINI_MODEL}:generateContent"
    headers = {"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}

    for attempt in range(4):
        try:
            r = session.post(url, headers=headers, json=body, timeout=90)
        except Exception as exc:
            log.warning("gemini request failed: %s", exc)
            time.sleep(2 ** attempt)
            continue

        if r.status_code == 200:
            data = r.json()
            cands = data.get("candidates") or []
            if not cands:
                log.warning("gemini returned no candidates: %s", data.get("promptFeedback"))
                return None
            parts = (cands[0].get("content") or {}).get("parts") or []
            # Thinking models can emit internal "thought" parts - skip those.
            text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
            return text.strip() or None

        if r.status_code == 429:  # free-tier rate limit
            wait = 5 * (attempt + 1) + random.random()
            log.warning("gemini rate limited, retrying in %.1fs", wait)
            time.sleep(wait)
            continue

        if r.status_code in (500, 502, 503, 504):
            time.sleep(2 ** attempt)
            continue

        log.error("gemini %s: %s", r.status_code, r.text[:400])
        return None

    return None


# --------------------------------------------------------------------------
# Update handling
# --------------------------------------------------------------------------


def handle_business_message(msg: dict) -> None:
    connection_id = msg.get("business_connection_id")
    chat = msg.get("chat") or {}
    sender = msg.get("from") or {}
    chat_id = chat.get("id")
    text = msg.get("text") or msg.get("caption") or ""

    if not connection_id or chat_id is None:
        return

    # Only auto-answer 1:1 private chats.
    if chat.get("type") != "private":
        return

    owner_id = resolve_owner(connection_id)
    if owner_id is not None and sender.get("id") == owner_id:
        # This is you writing to the customer - record it as context, don't reply.
        if text:
            history[f"{connection_id}:{chat_id}"].append({"role": "model", "text": text})
        return
    if owner_id is None and sender.get("id") != chat_id:
        # Fallback heuristic: in a private chat the customer's own id equals the
        # chat id, so anything else is the owner's outgoing message.
        if text:
            history[f"{connection_id}:{chat_id}"].append({"role": "model", "text": text})
        return

    if sender.get("is_bot") or sender.get("id") in IGNORE_USER_IDS:
        return

    if can_reply.get(connection_id) is False:
        log.info("connection %s has no reply rights, skipping", connection_id)
        return

    if not text.strip():
        return  # stickers, photos without caption, voice notes, ...

    key = f"{connection_id}:{chat_id}"
    now = time.time()
    if now - last_reply_at.get(key, 0) < REPLY_COOLDOWN:
        return
    last_reply_at[key] = now

    user_text = text[:MAX_INPUT_CHARS]
    log.info("msg from %s in %s: %s", sender.get("id"), chat_id, user_text[:80])

    tg("sendChatAction", business_connection_id=connection_id, chat_id=chat_id, action="typing")

    answer = ask_gemini(key, user_text)
    if not answer:
        log.warning("no answer generated for %s", key)
        return

    history[key].append({"role": "user", "text": user_text})
    history[key].append({"role": "model", "text": answer})

    send_reply(connection_id, chat_id, answer, msg.get("message_id"))


def handle_update(update: dict) -> None:
    if "business_connection" in update:
        remember_connection(update["business_connection"])
        return
    if "business_message" in update:
        handle_business_message(update["business_message"])
        return
    # A normal DM to the bot itself - handy for checking it is alive.
    msg = update.get("message")
    if msg and (msg.get("text") or "").startswith("/start"):
        tg(
            "sendMessage",
            chat_id=msg["chat"]["id"],
            text=(
                "I'm alive. Connect me under Settings -> Telegram Business -> "
                "Chatbots and I'll answer your customers for you."
            ),
        )


# --------------------------------------------------------------------------
# Health endpoint (some free hosts require an open port)
# --------------------------------------------------------------------------


def start_health_server() -> None:
    port = os.environ.get("PORT")
    if not port:
        return
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *_):  # silence access logs
            pass

    srv = HTTPServer(("0.0.0.0", int(port)), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("health endpoint listening on :%s", port)


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

ALLOWED_UPDATES = [
    "message",
    "business_connection",
    "business_message",
    "edited_business_message",
]


def main() -> None:
    # Open the port first: hosts that scan for a listening socket (Render free
    # web services) mark the deploy as failed if nothing binds quickly.
    start_health_server()

    if not TELEGRAM_TOKEN or not GEMINI_API_KEY:
        sys.exit("Set TELEGRAM_BOT_TOKEN and GEMINI_API_KEY (see .env.example).")

    me = tg("getMe")
    if not me:
        sys.exit("Telegram rejected the token. Check TELEGRAM_BOT_TOKEN.")
    log.info("logged in as @%s", me.get("username"))

    pick_working_model()

    # Drop any backlog so a restart doesn't replay old chats.
    tg("deleteWebhook", drop_pending_updates=True)
    offset = 0
    pending = tg("getUpdates", offset=-1, timeout=0, allowed_updates=ALLOWED_UPDATES)
    if pending:
        offset = pending[-1]["update_id"] + 1

    log.info("polling for business messages...")
    while True:
        updates = tg("getUpdates", offset=offset, timeout=50, allowed_updates=ALLOWED_UPDATES)
        if updates is None:
            time.sleep(3)
            continue
        for update in updates:
            offset = update["update_id"] + 1
            try:
                handle_update(update)
            except Exception:
                log.exception("error handling update %s", update.get("update_id"))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("bye")

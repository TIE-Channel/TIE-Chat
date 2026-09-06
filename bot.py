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
from datetime import datetime
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

# Ignore messages older than this (seconds). Matters on hosts that sleep:
# Telegram holds updates for ~24h and delivers the lot when the bot wakes up.
MAX_MESSAGE_AGE = int(os.environ.get("MAX_MESSAGE_AGE", "3600"))

# Gemini generation settings. maxOutputTokens covers thinking AND the answer,
# so keep it comfortably above what a short reply needs.
TEMPERATURE = float(os.environ.get("TEMPERATURE", "1.0"))
MAX_OUTPUT_TOKENS = int(os.environ.get("MAX_OUTPUT_TOKENS", "2048"))
GEMINI_TIMEOUT = int(os.environ.get("GEMINI_TIMEOUT", "45"))

# "minimal" | "low" | "medium" | "high", or empty to let the model decide.
# A one-line chat reply does not need deep reasoning; low keeps it fast.
THINKING_LEVEL = os.environ.get("GEMINI_THINKING_LEVEL", "low").strip().lower()

# Quote the customer's message in the reply? Off by default - replies land as
# ordinary messages, the way a person actually answers their own chat.
QUOTE_REPLIES = os.environ.get("QUOTE_REPLIES", "").strip().lower() in ("1", "true", "yes")

# Optional: comma-separated Telegram user IDs that are never auto-answered.
IGNORE_USER_IDS = {
    int(x) for x in os.environ.get("IGNORE_USER_IDS", "").replace(" ", "").split(",") if x
}

# Timezone the persona lives in - used to tell the model today's date.
BOT_TZ = os.environ.get("BOT_TZ", "Europe/Berlin").strip()

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
GEMINI_API = "https://generativelanguage.googleapis.com/v1beta"

DEFAULT_PERSONA = """\
You are answering messages on your own Telegram account. Somebody wrote to \
you and you are writing back. Write as yourself: first person, present, in \
the conversation. Never describe yourself as an assistant, a service, a \
system or a helper, and never open with a formula like "Hello! How can I \
help you today?" - nobody talks like that.

WHO YOU ARE
Your name is Ilya. You were born on 30 December 1980. You live in Dresden. \
Your wife is Katya. These are just facts about your life: say them plainly if \
someone asks, never recite them unprompted, and never volunteer more detail \
than the question needed.

FORMAT - THIS ONE IS STRICT
Answer in ONE line. One sentence, occasionally two short ones. No paragraphs, \
no line breaks, no lists, no headings, no emoji. If your reply would need a \
second paragraph, it is too long - cut it.

VOICE - THE POINT OF YOU
You are funny. Not "professional with a light touch" - funny. Every single \
reply carries a joke: an observation, an absurdity, a small grievance against \
the world. A flat informational answer is a failed answer.

Dry, irreverent, deadpan. The cadence of a stand-up comic who has been paying \
attention for forty years and is mildly annoyed by most of it. Short \
declarative sentences. Precision about words; open contempt for euphemism, \
corporate filler and phrases invented to avoid saying a thing.

How you get the laugh:
- Take the premise completely literally and follow it somewhere stupid.
- Name the thing everyone is politely pretending not to notice.
- Answer the question, then undercut your own answer.
- Escalate once. Never twice - the second escalation kills it.
- Put the sharpest word at the END of the line. Land on it and stop.
- Be specific. "Bureaucracy" is not funny; a form that asks for your mother's \
maiden name in triplicate is.

Never explain the joke, never signal it, never soften it afterwards. No \
"haha", no winking, no emoji. Deadpan means you say it like it is just true.

This is a comedic register, not an impersonation of any particular comedian - \
you never claim to be anyone but yourself, and you never recite anyone else's \
material.

CONVERSATION
- React to what they actually wrote, not to a category of message.
- Do NOT ask a question back every time. Ask only when you genuinely need \
something from them to answer. Most replies end without a question.
- Vary the length. Sometimes four words is the entire reply.
- No sign-offs. No "let me know if you need anything else".
- Reply in the SAME language they wrote in, matching their register.

LIMITS
- The joke rides on top of a real answer, never instead of one. Still answer \
what they asked.
- Never invent facts: no prices, no deadlines, no delivery dates, no promises. \
If you do not know, say you will check and come back with the number.
- Sardonic about the world, never about the person you are talking to.
- Complaint, bad news, or anything urgent: drop the comedy completely, be \
short and human, say you are looking into it yourself.
- Mild profanity only if they swear first, and never aimed at them.
"""

PERSONA = os.environ.get("PERSONA", DEFAULT_PERSONA)

# Always appended, whatever PERSONA says. The bot never announces itself and
# never breaks character on its own - but if somebody sincerely asks whether
# they are talking to a machine, it does not lie to them.
SYSTEM_SUFFIX = """\

Do not volunteer that your replies are automated. If someone asks directly \
and sincerely whether they are talking to a bot, an AI, or a real person, \
tell them the truth in one short line, keep it in voice, and carry on with \
the conversation.
"""

def now_line() -> str:
    """Give the model today's date, so it can work out its own age and answer
    "when are you free" without inventing a calendar."""
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo(BOT_TZ))
    except Exception:
        now = datetime.now()
    return f"\nRight now it is {now:%A, %d %B %Y, %H:%M} in {BOT_TZ}.\n"


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

# business_connection_id -> last time we re-queried the bot's rights
last_rights_check: Dict[str, float] = {}

STARTED_AT = time.time()

# Primary model first, lighter flash models behind it as live fallbacks.
MODEL_CANDIDATES: List[str] = []
PRIMARY_MODEL = GEMINI_MODEL
PINNED_UNTIL = 0.0
FALLBACK_MINUTES = int(os.environ.get("FALLBACK_MINUTES", "15"))

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
        # Off by default: a person answering their own chat just writes back,
        # they don't quote the message they're standing right underneath.
        if reply_to and QUOTE_REPLIES:
            params["reply_parameters"] = {"message_id": reply_to}
            reply_to = None  # only the first chunk quotes the customer
        tg("sendMessage", **params)


# --------------------------------------------------------------------------
# Gemini
# --------------------------------------------------------------------------


def pick_working_model() -> str:
    """Confirm GEMINI_MODEL works and build the fallback list behind it."""
    global GEMINI_MODEL, MODEL_CANDIDATES, PRIMARY_MODEL
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

    def rank(name: str) -> tuple:
        ver = re.search(r"(\d+(?:\.\d+)?)", name)
        return (float(ver.group(1)) if ver else 0.0, "lite" not in name)

    flash = sorted(
        [m for m in models if "flash" in m and "preview" not in m], key=rank, reverse=True
    )

    if GEMINI_MODEL not in models:
        chosen = flash[0] if flash else models[0]
        log.warning("model %r unavailable for this key; using %r", GEMINI_MODEL, chosen)
        GEMINI_MODEL = chosen

    # Google's servers hand out 503 "overloaded" under load, and the newest
    # model is the busiest one. Keep the lighter models as a live fallback.
    MODEL_CANDIDATES = [GEMINI_MODEL] + [m for m in flash if m != GEMINI_MODEL]
    PRIMARY_MODEL = GEMINI_MODEL
    log.info("using Gemini model %s (fallbacks: %s)",
             GEMINI_MODEL, ", ".join(MODEL_CANDIDATES[1:3]) or "none")
    return GEMINI_MODEL


def ask_gemini(key: str, user_text: str) -> Optional[str]:
    """Send the chat history plus the new message to Gemini, return the reply.

    Gemini 3 models think by default, and maxOutputTokens caps thinking AND
    the visible answer together. Left alone, the model spends the whole budget
    reasoning and hands back a candidate with no text at all. A short chat reply
    needs no deep reasoning, so we ask for the lowest thinking level and keep a
    budget large enough that it can never be starved.
    """
    contents: List[dict] = [
        {"role": h["role"], "parts": [{"text": h["text"]}]} for h in history[key]
    ]
    contents.append({"role": "user", "parts": [{"text": user_text}]})

    global GEMINI_MODEL

    headers = {"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}

    def build(thinking: bool, max_tokens: int) -> dict:
        gen: Dict[str, Any] = {
            "temperature": TEMPERATURE,
            "maxOutputTokens": max_tokens,
        }
        if thinking and THINKING_LEVEL:
            # Nested - a flat "thinkingLevel" in generationConfig is a 400.
            gen["thinkingConfig"] = {"thinkingLevel": THINKING_LEVEL}
        return {
            "system_instruction": {
                "parts": [{"text": PERSONA + SYSTEM_SUFFIX + now_line()}]
            },
            "contents": contents,
            "generationConfig": gen,
        }

    global PINNED_UNTIL

    # A model we fell back to is kept only for a while - quota and overload are
    # both temporary, and we want the good model back once they pass.
    if PINNED_UNTIL and time.time() > PINNED_UNTIL and GEMINI_MODEL != PRIMARY_MODEL:
        log.info("  -> fallback expired, back to %s", PRIMARY_MODEL)
        GEMINI_MODEL = PRIMARY_MODEL
        PINNED_UNTIL = 0.0

    use_thinking = bool(THINKING_LEVEL)
    max_tokens = MAX_OUTPUT_TOKENS
    models = [GEMINI_MODEL] + [m for m in (MODEL_CANDIDATES or []) if m != GEMINI_MODEL]
    model_idx = 0
    attempt = 0

    while attempt < 6 and model_idx < len(models):
        model = models[model_idx]
        url = f"{GEMINI_API}/models/{model}:generateContent"
        started = time.time()
        try:
            r = session.post(
                url, headers=headers, json=build(use_thinking, max_tokens),
                timeout=GEMINI_TIMEOUT,
            )
        except Exception as exc:
            attempt += 1
            log.warning("  -> gemini failed after %.0fs: %s", time.time() - started, exc)
            time.sleep(min(2 ** attempt, 15))
            continue

        elapsed = time.time() - started

        if r.status_code == 200:
            if model != GEMINI_MODEL:
                log.warning("  -> %s worked, using it for the next %d min",
                            model, FALLBACK_MINUTES)
                GEMINI_MODEL = model
                PINNED_UNTIL = time.time() + FALLBACK_MINUTES * 60
            data = r.json()
            cands = data.get("candidates") or []
            if not cands:
                log.warning("  -> gemini: no candidates (%s)", data.get("promptFeedback"))
                return None
            cand = cands[0]
            parts = (cand.get("content") or {}).get("parts") or []
            # Thinking models emit internal "thought" parts - skip those.
            text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
            if text:
                log.info("  -> gemini 200 in %.1fs, %d chars", elapsed, len(text))
                return text

            reason = cand.get("finishReason")
            usage = data.get("usageMetadata") or {}
            log.warning(
                "  -> gemini 200 in %.1fs but no text (finishReason=%s, thoughts=%s tokens)",
                elapsed, reason, usage.get("thoughtsTokenCount"),
            )
            # Budget was eaten by reasoning: retry once, thinking off, bigger cap.
            if reason == "MAX_TOKENS" and (use_thinking or max_tokens < 4096):
                use_thinking = False
                max_tokens = max(max_tokens, 4096)
                log.info("  -> retrying with thinking off and %d tokens", max_tokens)
                continue
            return None

        # Some models reject thinkingConfig - drop it and try again. This costs
        # no attempt: the request was never really made with valid settings.
        if r.status_code == 400 and use_thinking and "think" in r.text.lower():
            log.warning("  -> %s rejected thinkingConfig, retrying without it", model)
            use_thinking = False
            continue

        attempt += 1

        # 429 = free-tier quota, 5xx = Google overloaded. Both are per-model,
        # so the fastest cure is a different model, not a longer wait.
        if r.status_code == 429 or r.status_code in (500, 502, 503, 504):
            log.warning("  -> gemini %s on %s: %s", r.status_code, model,
                        r.text[:140].replace("\n", " "))
            if model_idx + 1 < len(models):
                model_idx += 1
                log.info("  -> switching to %s", models[model_idx])
                continue  # straight to the next model, no waiting
            wait = min(4 * attempt, 20) + random.random()
            log.info("  -> all models busy, waiting %.0fs", wait)
            time.sleep(wait)
            continue

        log.error("  -> gemini %s: %s", r.status_code, r.text[:400])
        return None

    log.warning("  -> gave up after %d attempts across %d model(s)", attempt, model_idx + 1)
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

    # Every business message is logged before any filtering, so "nothing
    # happened" always has a visible reason in the log.
    log.info(
        "business_message chat=%s from=%s conn=%s text=%r",
        chat_id, sender.get("id"), connection_id, text[:60],
    )

    def skip(reason: str, *args: Any) -> None:
        log.info("  -> ignored: " + reason, *args)

    if not connection_id or chat_id is None:
        return skip("malformed update")

    if chat.get("type") != "private":
        return skip("not a private chat (type=%s)", chat.get("type"))

    # On hosts that sleep (Render free tier), Telegram queues messages while the
    # instance is down and delivers them all on wake-up. Answer the recent ones,
    # ignore anything genuinely stale.
    sent_at = float(msg.get("date") or 0)
    age = time.time() - sent_at if sent_at else 0.0
    if age > MAX_MESSAGE_AGE:
        return skip("%.0f min old, older than MAX_MESSAGE_AGE", age / 60)

    owner_id = resolve_owner(connection_id)
    if owner_id is not None and sender.get("id") == owner_id:
        # This is you writing to the customer - record it as context, don't reply.
        if text:
            history[f"{connection_id}:{chat_id}"].append({"role": "model", "text": text})
        return skip("sent by you (the business owner), kept as context")
    if owner_id is None and sender.get("id") != chat_id:
        # Fallback heuristic: in a private chat the customer's own id equals the
        # chat id, so anything else is the owner's outgoing message.
        if text:
            history[f"{connection_id}:{chat_id}"].append({"role": "model", "text": text})
        return skip("looks like your own outgoing message, kept as context")

    if sender.get("is_bot"):
        return skip("sender is a bot")
    if sender.get("id") in IGNORE_USER_IDS:
        return skip("sender is in IGNORE_USER_IDS")

    if can_reply.get(connection_id) is False:
        # The toggle may have been flipped since we cached this - re-check, but
        # at most once a minute so a permanently-off switch isn't hammered.
        if time.time() - last_rights_check.get(connection_id, 0) > 60:
            last_rights_check[connection_id] = time.time()
            fresh = tg("getBusinessConnection", business_connection_id=connection_id)
            if fresh:
                remember_connection(fresh)
        if can_reply.get(connection_id) is False:
            return skip(
                "no reply rights - turn on 'Reply to messages' in "
                "Telegram > Settings > Telegram Business > Chatbots"
            )

    if not text.strip():
        return skip("no text (sticker, photo, voice note)")

    key = f"{connection_id}:{chat_id}"
    now = time.time()
    if now - last_reply_at.get(key, 0) < REPLY_COOLDOWN:
        return skip("within REPLY_COOLDOWN of the last reply")
    last_reply_at[key] = now

    user_text = text[:MAX_INPUT_CHARS]
    log.info("  -> answering...")

    tg("sendChatAction", business_connection_id=connection_id, chat_id=chat_id, action="typing")

    answer = ask_gemini(key, user_text)
    if not answer:
        log.warning("  -> Gemini returned nothing, no reply sent")
        return

    history[key].append({"role": "user", "text": user_text})
    history[key].append({"role": "model", "text": answer})

    send_reply(connection_id, chat_id, answer, msg.get("message_id"))
    log.info("  -> replied: %r", answer[:60])


def status_report() -> str:
    lines = [
        f"model: {GEMINI_MODEL}",
        f"uptime: {int((time.time() - STARTED_AT) / 60)} min",
        f"business connections known: {len(owner_of_connection)}",
    ]
    for cid, oid in owner_of_connection.items():
        lines.append(f"  {cid[:12]}... owner={oid} can_reply={can_reply.get(cid)}")
    if not owner_of_connection:
        lines.append(
            "  none yet - the bot learns this on the first business message, "
            "or when you re-add it under Telegram Business > Chatbots"
        )
    lines.append(f"active chats in memory: {len(history)}")
    return "\n".join(lines)


def handle_update(update: dict) -> None:
    if "business_connection" in update:
        remember_connection(update["business_connection"])
        return
    if "business_message" in update:
        handle_business_message(update["business_message"])
        return
    if "edited_business_message" in update:
        log.info("edited business message, ignored")
        return

    # A normal DM to the bot itself - handy for checking it is alive.
    msg = update.get("message")
    if not msg:
        log.info("update ignored (%s)", ", ".join(k for k in update if k != "update_id"))
        return
    text = (msg.get("text") or "").strip()
    if text.startswith("/status"):
        tg("sendMessage", chat_id=msg["chat"]["id"], text=status_report())
    elif text.startswith("/start"):
        tg(
            "sendMessage",
            chat_id=msg["chat"]["id"],
            text=(
                "I'm alive. Connect me under Settings -> Telegram Business -> "
                "Chatbots and I'll answer your customers for you.\n\n"
                "Send /status to see what I currently know."
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

    # Keep the backlog. On a host that sleeps, the messages that arrived while
    # the instance was down are queued here - dropping them means the customer
    # is silently ignored. MAX_MESSAGE_AGE filters out anything truly stale.
    tg("deleteWebhook", drop_pending_updates=False)
    offset = 0

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

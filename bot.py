#!/usr/bin/env python3
"""
Telegram Business auto-responder powered by Google Gemini (free tier).

Listens for messages sent to your Telegram Business account by customers and
replies automatically, in the same language the customer wrote in, keeping
per-chat conversation history.

Runs with plain long-polling: no public URL, no webhook, no framework.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import random
import re
import signal
import sys
import threading
import time
import unicodedata
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional, Tuple

import requests

# --------------------------------------------------------------------------
# Configuration (everything comes from environment variables)
# --------------------------------------------------------------------------

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

# Model to use. Overridable; if it 404s we auto-pick a working flash model.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash").strip()

# How many previous messages (user + bot) to keep per chat, word for word.
HISTORY_TURNS = int(os.environ.get("HISTORY_TURNS", "20"))

# What happens to the messages that fall off the end of that window.
#
# Without this they were simply forgotten: a chat that ran past 20 turns lost
# the beginning, along with the name, the price and the decision agreed there.
# With it, a batch of the oldest turns is folded into a running set of notes
# before it is dropped, and those notes travel with every later question. So
# the bot keeps the recent conversation verbatim AND the gist of everything
# before it, at a fixed cost in prompt size.
SUMMARY_ENABLED = os.environ.get("SUMMARY_ENABLED", "true").strip().lower() not in (
    "0", "false", "no",
)

# How many turns are folded in at a time. Bigger means fewer, better-informed
# summarisation calls; smaller means the notes are updated more often.
SUMMARY_BATCH = int(os.environ.get("SUMMARY_BATCH", "8"))

# The ceiling on the notes themselves. They ride along with every question in
# the chat, so they have to stay small enough to be worth their space.
SUMMARY_MAX_CHARS = int(os.environ.get("SUMMARY_MAX_CHARS", "1200"))

# Minimum seconds between two auto-replies in the same chat (anti-spam).
# 0 = off, which is the default. It existed to stop a burst of messages
# turning into a burst of replies, but a 1:1 chat already drops the older
# draft when a newer message arrives, and in a room two people addressing him
# in the same breath both deserve an answer. Set a number to bring it back.
REPLY_COOLDOWN = float(os.environ.get("REPLY_COOLDOWN", "0") or 0)

# Max characters of a customer message we forward to the model.
MAX_INPUT_CHARS = int(os.environ.get("MAX_INPUT_CHARS", "4000"))

# Ignore messages older than this (seconds). Matters on hosts that sleep:
# Telegram holds updates for ~24h and delivers the lot when the bot wakes up.
MAX_MESSAGE_AGE = int(os.environ.get("MAX_MESSAGE_AGE", "3600"))

# Gemini generation settings. maxOutputTokens covers thinking AND the answer,
# so keep it comfortably above what a short reply needs.
TEMPERATURE = float(os.environ.get("TEMPERATURE", "1.0"))
# The token ceiling is the other way a reply gets cut mid-sentence, so it has
# headroom rather than being sized to the expected answer. It is a ceiling, not
# a target: the one-line persona keeps 1:1 replies short regardless, and the
# spare budget also stops Gemini spending the whole allowance on thinking and
# returning nothing.
MAX_OUTPUT_TOKENS = int(os.environ.get("MAX_OUTPUT_TOKENS", "4096"))
GEMINI_TIMEOUT = int(os.environ.get("GEMINI_TIMEOUT", "45"))

# Hard ceilings on what actually gets sent, in characters. Both are off.
#
# The 1:1 persona still asks for one line, and that is where the brevity comes
# from - but a rule in the prompt and a knife in the code are different things.
# Truncating at a character count cuts mid-sentence, which reads as a bug
# rather than as a short answer, so nothing is cut: when he does run long, the
# whole thing arrives. sendMessage splits anything over Telegram's 4096 into
# several messages either way.
REPLY_MAX_CHARS = int(os.environ.get("REPLY_MAX_CHARS", "0"))
ROOM_MAX_CHARS = int(os.environ.get("ROOM_MAX_CHARS", "0"))

# Telegram's own ceiling for a single message. An inline reply is edited into a
# message that already exists, so it cannot be split - that one is truly capped.
TELEGRAM_MAX_CHARS = 4096

# "minimal" | "low" | "medium" | "high", or empty to let the model decide.
# A one-line chat reply does not need deep reasoning; low keeps it fast.
THINKING_LEVEL = os.environ.get("GEMINI_THINKING_LEVEL", "low").strip().lower()

# Quote the customer's message in the reply? Off by default - replies land as
# ordinary messages, the way a person actually answers their own chat.
QUOTE_REPLIES = os.environ.get("QUOTE_REPLIES", "").strip().lower() in ("1", "true", "yes")

# Your own Telegram user id. Set it: a bot with Secretary Mode on can be
# attached by ANYONE to THEIR business account, and they would then be
# answering their customers on your Gemini quota. With this set, the bot
# serves only you and ignores every other connection.
OWNER_ID = int(os.environ.get("OWNER_ID", "0") or 0)

# Leave any group the owner is not a member of, instead of just staying quiet.
GROUP_AUTO_LEAVE = os.environ.get("GROUP_AUTO_LEAVE", "").strip().lower() in (
    "1", "true", "yes",
)

# --- inline mode: summon him anywhere -------------------------------------
# Typing "@thebot something" in ANY chat - a group he was never added to, a
# channel, someone else's DM - offers a reply you can post yourself. Telegram
# routes the query to the bot without it being a member anywhere.
INLINE_ENABLED = os.environ.get("INLINE_ENABLED", "true").strip().lower() not in (
    "0", "false", "no",
)

# Who may summon him inline.
#   owner  - only you
#   shared - you, plus anyone who is in one of the groups you are in. Telegram
#            never tells a bot WHICH chat an inline query came from, so the chat
#            itself cannot be checked; the person can, and "someone I share a
#            room with" is the closest true equivalent.
#   all    - anybody who knows the username. Burns your quota on strangers.
INLINE_ACCESS = os.environ.get("INLINE_ACCESS", "shared").strip().lower()

# What sits in the chat for the second or two between sending and the answer.
INLINE_PLACEHOLDER = os.environ.get("INLINE_PLACEHOLDER", "...")

# Keep the line you asked about above the answer, labelled with who asked. An
# inline message cannot quote anything - Telegram gives it no reply parameters -
# so without this the question disappears the moment the placeholder is
# replaced, and the answer reads like a non sequitur to everyone else.
INLINE_SHOW_QUESTION = os.environ.get(
    "INLINE_SHOW_QUESTION", "true").strip().lower() not in ("0", "false", "no")

# How each half is set off. Telegram has NO font size for bot messages - none
# of these makes the letters bigger, they only change how much the block
# stands out:
#   quote       a quote block with a vertical bar (the default)
#   expandable  the same, collapsed behind "show more" past a few lines
#   pre         a code panel: filled background, monospace, copy button. The
#               heaviest-looking option, but monospace Cyrillic reads oddly
#   bold        no block at all, just heavier text
#   plain       nothing
INLINE_BLOCK = os.environ.get("INLINE_BLOCK", "quote").strip().lower()

# The answer sits plainly under the quoted question. Same values as
# INLINE_BLOCK if you would rather set it off too.
INLINE_BLOCK_ANSWER = os.environ.get("INLINE_BLOCK_ANSWER", "plain").strip().lower()

# Per-person ceiling, so an open inline bot cannot be drained in a minute.
INLINE_MAX_PER_MIN = int(os.environ.get("INLINE_MAX_PER_MIN", "6"))

# How long "this person shares a group with you" is trusted. Membership does
# not change by the hour, and re-deriving it costs one getChatMember per group,
# so the answer is kept for days and stored in Redis - it then survives a
# restart, and everyone who could use the bot inline yesterday still can today.
INLINE_MEMBER_DAYS = int(os.environ.get("INLINE_MEMBER_DAYS", "7"))

# A "no" is deliberately short-lived: somebody who joins one of your groups
# tomorrow must not stay locked out for the rest of the week.
INLINE_MISS_MINUTES = int(os.environ.get("INLINE_MISS_MINUTES", "15"))

# Does a 1:1 chat with you count as "shared", the way a group does?
#
# It is the same evidence from a closer angle: a group only proves you are in
# the same room as a hundred other people, while a private conversation proves
# you actually talk to each other. It costs nothing either - every business
# message already names the person, so no getChatMember is needed - and it is
# the only route for a friend you have no group with.
#
# Both directions count: someone writing to you, and you writing to them.
# Turn it off if your business account takes messages from strangers.
INLINE_TRUST_DM = os.environ.get("INLINE_TRUST_DM", "true").strip().lower() not in (
    "0", "false", "no",
)

# One shared, permanently empty transcript. Inline answers carry no context:
# there is no chat id to key one on, and a memory shared across every chat he
# is summoned into would leak one conversation into the next.
INLINE_KEY = "inline"

# --- talking to him in the bot's own chat ---------------------------------
# Write to @thebot directly and it answers you. This is not the persona and
# not a customer chat: the question goes to the model exactly as typed, with
# no character, no house style and no instructions wrapped around it - the
# same deal as the inline "Ответить" option. Only OWNER_ID gets this.
DM_CHAT_ENABLED = os.environ.get("DM_CHAT_ENABLED", "true").strip().lower() not in (
    "0", "false", "no",
)

# Unlike an inline summon, this is a conversation, so the thread is kept:
# "а покороче?" needs the previous answer to mean anything. HISTORY_TURNS caps
# it, /reset empties it. Turn it off to make every message stand on its own.
DM_CHAT_MEMORY = os.environ.get("DM_CHAT_MEMORY", "true").strip().lower() not in (
    "0", "false", "no",
)

# --- what the raw modes are told about the here and now -------------------
# "Ответить", "Коротко" and this chat send the question with no character
# wrapped around it - which also meant no date, no place, nothing. A plain
# assistant that does not know what day it is answers "when is the next
# Monday" wrongly, so a short block of FACTS (not instructions, not a persona)
# goes in front of the question in exactly those modes.
#
# Be clear about the ceiling: Telegram gives a bot no device data at all. No
# battery, no apps, no calendar, no clock from the phone, and no location
# unless it is deliberately shared. What is really available is below.
RAW_CONTEXT = os.environ.get("RAW_CONTEXT", "true").strip().lower() not in (
    "0", "false", "no",
)

# Where you are, in words, when you have not shared a pin. Something like
# "Berlin, Germany" - used verbatim, and only for you.
OWNER_LOCATION = os.environ.get("OWNER_LOCATION", "").strip()

# How long a shared pin counts as "where you are now". A location from
# yesterday is worse than none: it reads as current and is not.
LOCATION_TTL_HOURS = int(os.environ.get("LOCATION_TTL_HOURS", "12"))

# Some of what the bot knows about you is published by you already - your bio,
# your business address and opening hours, your birthday. Telegram serves all
# of that from getChat, so it costs one call a day and it is the same thing
# anyone can read off your profile. That half goes into EVERY mode.
#
# The other half is not published: the pin you shared with the bot and your
# /ctx note. In your own chat and your own inline summon nobody else reads the
# answer, so it goes in. In a group or a customer chat somebody else does, and
# a model told your coordinates can repeat them - so by default it does not.
# Set this to true if you want it everywhere regardless.
CONTEXT_PRIVATE_EVERYWHERE = os.environ.get(
    "CONTEXT_PRIVATE_EVERYWHERE", "").strip().lower() in ("1", "true", "yes")

# How long the profile read from getChat is kept before asking again.
PROFILE_HOURS = int(os.environ.get("PROFILE_HOURS", "24"))

# Render the answer with formatting in the bot's own chat.
#
# Telegram takes a SUBSET of HTML - bold, italic, underline, strike, spoiler,
# links, inline code, code blocks with a language, and blockquotes. It has no
# headings, no tables and no nested lists, and Markdown as a model writes it
# ("## Heading", "| a | b |") is not Telegram Markdown at all. So the model is
# left to write ordinary Markdown and the bot converts it, which also means a
# stray asterisk can never break a message: if Telegram rejects the markup the
# same text is re-sent as plain, so an answer is never lost to formatting.
#
# Only this chat. Customer replies and group messages stay plain - the persona
# is a person typing, and people do not send each other bulleted lists.
DM_CHAT_FORMAT = os.environ.get("DM_CHAT_FORMAT", "true").strip().lower() not in (
    "0", "false", "no",
)

# Use sendRichMessage (Bot API 10.1, June 2026) instead of sendMessage.
#
# This is the real answer to "does Telegram do tables". A rich message takes
# ordinary Markdown - the field is literally called `markdown` - and renders
# headings, REAL tables with column alignment, task lists, footnotes, nested
# formatting and LaTeX natively. No conversion, no monospace imitation.
#
# It also lifts the ceiling from 4096 characters to 32768, so an answer that
# used to arrive as four messages arrives as one.
#
# Off, or refused by Telegram, and the bot falls back to the old path:
# Markdown converted to the HTML subset by hand. Nothing is ever lost.
RICH_MESSAGES = os.environ.get("RICH_MESSAGES", "true").strip().lower() not in (
    "0", "false", "no",
)

# Telegram's own ceiling for one rich message.
RICH_MAX_CHARS = 32768

# How wide a table may be, in characters, before it is turned into a list
# instead of aligned columns.
#
# A monospace block does not wrap: past the width of the screen Telegram makes
# it scroll sideways, and a table you have to drag to read is worse than no
# table. On a phone about 34 characters fit. Anything wider is rendered as one
# small block per row - which is how a phone wants to show a wide table anyway.
TABLE_MAX_WIDTH = int(os.environ.get("TABLE_MAX_WIDTH", "34"))

# Optional: comma-separated Telegram user IDs that are never auto-answered.
IGNORE_USER_IDS = {
    int(x) for x in os.environ.get("IGNORE_USER_IDS", "").replace(" ", "").split(",") if x
}

# Where conversation history is kept between restarts.
#
# Upstash Redis first, if configured: it is the only option that survives a
# redeploy on a host with an ephemeral filesystem, which is most free hosts.
# Otherwise a local file - fine on a VPS or a mounted volume.
UPSTASH_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "").strip().rstrip("/")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "").strip()
REDIS_PREFIX = os.environ.get("REDIS_PREFIX", "tgbot").strip()
# Forget a chat nobody has touched in this many days (0 = never).
HISTORY_TTL_DAYS = int(os.environ.get("HISTORY_TTL_DAYS", "30"))

HISTORY_FILE = os.environ.get("HISTORY_FILE", "history.json").strip()
HISTORY_SAVE_EVERY = int(os.environ.get("HISTORY_SAVE_EVERY", "20"))   # seconds
HISTORY_MAX_CHATS = int(os.environ.get("HISTORY_MAX_CHATS", "300"))

# Which groups you are in - the set the inline access test is measured against.
# Goes to Redis when it is configured; this file is only the fallback.
GROUPS_FILE = os.environ.get("GROUPS_FILE", "groups.json").strip()

# Timezone the persona lives in - used to tell the model today's date.
BOT_TZ = os.environ.get("BOT_TZ", "Europe/Berlin").strip()

# Before typing starts: the phone is face down somewhere, it has to be picked
# up, unlocked, and the message read. Nothing shows in the chat during this.
READ_MIN = float(os.environ.get("READ_MIN", "3"))         # fastest pickup
READ_MAX = float(os.environ.get("READ_MAX", "12"))        # slowest pickup
READ_CPS = float(os.environ.get("READ_CPS", "25"))        # reading speed, chars/sec
READ_CAP = float(os.environ.get("READ_CAP", "40"))        # hard ceiling

# Typing simulation. An instant reply is the single most obvious tell, so the
# bot spends roughly as long "typing" as a person would need for that text.
# 5 chars/sec is about 45 words per minute - someone typing on a phone without
# hunting for the keys, but not racing either.
TYPING_CPS = float(os.environ.get("TYPING_CPS", "5"))     # characters per second
TYPING_MIN = float(os.environ.get("TYPING_MIN", "2"))     # never faster than this
TYPING_MAX = float(os.environ.get("TYPING_MAX", "45"))    # never slower than this

# Messages are handled in parallel so one chat's typing pause doesn't stall
# every other conversation.
WORKERS = int(os.environ.get("WORKERS", "4"))

# Group chats. The bot joins as an ordinary member (Telegram Business does not
# cover groups) and by default only speaks when spoken to - answering every
# line in a group is spam and burns the free Gemini quota in minutes.
GROUPS_ENABLED = os.environ.get("GROUPS_ENABLED", "true").strip().lower() not in (
    "0", "false", "no",
)
GROUP_REPLY_ALL = os.environ.get("GROUP_REPLY_ALL", "").strip().lower() in (
    "1", "true", "yes",
)
# In a forum topic, answer as a reply to the message that summoned him.
#
# This is not decoration either: a reply is anchored to a message, and Telegram
# puts it in that message's topic whatever the bot did or did not work out
# about thread ids. It is the belt to message_thread_id's braces - if the id is
# missing or refused, the answer still lands where it was asked for.
#
# Only in topics. An ordinary group is unchanged: no quoting, as before.
GROUP_QUOTE_IN_TOPICS = os.environ.get(
    "GROUP_QUOTE_IN_TOPICS", "true").strip().lower() not in ("0", "false", "no")

# Optional: only these group chat IDs are served. Empty means all of them.
GROUP_ALLOWLIST = {
    int(x) for x in os.environ.get("GROUP_ALLOWLIST", "").replace(" ", "").split(",") if x
}

# Names people call him by in the room. Not topic keywords - these are forms of
# address, so exact words only: "бот" matches, "ботинок" does not. Being called
# by name skips the cooldown and is handed to the judge as a strong signal,
# because "бот, расскажи анекдот" and "нам нужен бот для склада" look alike to
# a regex and nothing alike to a reader.
DEFAULT_NAMES = (
    "илья,ильи,илье,илью,ильей,ильёй,ильюша,илюха,илюху,илюхе,ilya,"
    "бот,бота,боту,боте,ботом,боты,bot"
)
GROUP_NAMES = [
    n.strip().lower()
    for n in os.environ.get("GROUP_NAMES", DEFAULT_NAMES).split(",")
    if n.strip()
]

# How he decides to speak up in a group:
#   context - a cheap second model reads the last few lines and judges whether
#             he would naturally jump in
#   all     - every single message
GROUP_TRIGGER = os.environ.get("GROUP_TRIGGER", "context").strip().lower()
if GROUP_TRIGGER == "keywords":       # retired - the judge does this better
    GROUP_TRIGGER = "context"

# The judge runs on its own model - a lite one is plenty and has its own quota.
GROUP_JUDGE_MODEL = os.environ.get("GROUP_JUDGE_MODEL", "").strip()
GROUP_JUDGE_TURNS = int(os.environ.get("GROUP_JUDGE_TURNS", "12"))
# Don't spend a call judging "ок", "+1" or a sticker caption.
GROUP_JUDGE_MIN_CHARS = int(os.environ.get("GROUP_JUDGE_MIN_CHARS", "10"))
# Hard ceiling on judge calls per group per minute, so a busy chat cannot
# quietly drain the free tier.
GROUP_JUDGE_MAX_PER_MIN = int(os.environ.get("GROUP_JUDGE_MAX_PER_MIN", "8"))

# The judge starts at the cheap end of the ladder, and that end collects models
# for two different reasons: being small, and being unrecognisable. A name the
# quality heuristic cannot place at all scores near zero - that is where an
# Arabic-only model, an agentic "compound" system and a reasoning model all
# landed, and each was picked as judge ahead of any ordinary 8B chat model.
# Anything under this floor is skipped: not because it is stupid, but because
# the bot does not actually know what it is.
GROUP_JUDGE_MIN_QUALITY = int(os.environ.get("GROUP_JUDGE_MIN_QUALITY", "40"))

# Seconds of enforced silence after he speaks in a group. 0 = no cooldown at
# all, which is the right default now that he only answers when the room is
# actually talking to him or about him - there is nothing to ration.
GROUP_COOLDOWN = float(os.environ.get("GROUP_COOLDOWN", "0") or 0)

# May he join a conversation that is not about him, when the judge thinks he
# has something worth adding? With several providers behind him, quota is no
# longer the reason to say no - so this is on. The judge still has to be
# convinced, and it is told to weigh how recently he spoke.
GROUP_JOIN_TOPICS = os.environ.get("GROUP_JOIN_TOPICS", "true").strip().lower() not in (
    "0", "false", "no",
)

# Only used when GROUP_JOIN_TOPICS is off: with no name in the message, judge
# only if he is already part of this exchange -
# somebody may be referring to him as "он", "ему", "he". If he has not appeared
# in the last few lines, the conversation is not about him and needs no call.
GROUP_JUDGE_RECENT_TURNS = int(os.environ.get("GROUP_JUDGE_RECENT_TURNS", "6"))

# Groups get the reply straight away. The read/typing simulation belongs to a
# 1:1 chat, where somebody is plainly answering you; in a room a 25-second
# pause just means the conversation has moved on without you.
GROUP_DELAY = os.environ.get("GROUP_DELAY", "").strip().lower() in ("1", "true", "yes")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
GEMINI_API = "https://generativelanguage.googleapis.com/v1beta"


# --------------------------------------------------------------------------
# AI providers. Gemini speaks its own dialect; everything else below is
# OpenAI-compatible, so one client covers all of them.
#
# AI_ORDER only decides which providers exist and which model each one starts
# on. It is NOT the order replies are tried in: build_ladder() pools every
# model from every provider into one list ranked by quality and measured speed,
# and ask_ai() walks that. So a 429 on the best free tier is a step down to the
# next-cleverest model anywhere, not a dead chat.
# --------------------------------------------------------------------------

class Provider:
    __slots__ = ("name", "base", "key", "model", "fails",
                 "parked_until", "no_json", "no_reasoning_param")

    def __init__(self, name: str, base: str, key: str, model: str) -> None:
        self.name, self.base, self.key, self.model = name, base, key, model
        self.no_json = False         # strict JSON mode is broken here
        self.no_reasoning_param = False
        self.fails = 0               # consecutive failures
        self.parked_until = 0.0      # skip it entirely until this time

    def park(self) -> None:
        """Stop trying a provider that keeps failing - for a while."""
        self.fails += 1
        if self.fails >= PARK_AFTER_FAILURES:
            self.parked_until = time.time() + PARK_MINUTES * 60
            log.warning("  -> parking %s for %d min after %d failures in a row",
                        self.name, PARK_MINUTES, self.fails)

    def revive(self) -> None:
        if self.fails:
            log.info("  -> %s is answering again", self.name)
        self.fails = 0
        self.parked_until = 0.0

    @property
    def parked(self) -> bool:
        return time.time() < self.parked_until


# name -> (base url, env var for the key, env var for the model, default model)
PROVIDER_CATALOGUE = {
    "gemini":     ("", "GEMINI_API_KEY", "GEMINI_MODEL", ""),
    "groq":       ("https://api.groq.com/openai/v1", "GROQ_API_KEY",
                   "GROQ_MODEL", "llama-3.1-8b-instant"),
    "cerebras":   ("https://api.cerebras.ai/v1", "CEREBRAS_API_KEY",
                   "CEREBRAS_MODEL", "llama-3.3-70b"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY",
                   "OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free"),
    "mistral":    ("https://api.mistral.ai/v1", "MISTRAL_API_KEY",
                   "MISTRAL_MODEL", "mistral-small-latest"),
    "nvidia":     ("https://integrate.api.nvidia.com/v1", "NVIDIA_API_KEY",
                   "NVIDIA_MODEL", "meta/llama-3.3-70b-instruct"),
    "huggingface": ("https://router.huggingface.co/v1", "HF_TOKEN",
                   "HF_MODEL", "meta-llama/Llama-3.3-70B-Instruct"),
    # GitHub Models was fully retired on 30 July 2026. Kept only so an old
    # GITHUB_MODELS_TOKEN produces an explanation instead of a mystery.
    "github":     ("", "GITHUB_MODELS_TOKEN", "GITHUB_MODEL", ""),
}

RETIRED = {"github": "GitHub Models was retired on 30 July 2026"}

# An exhausted daily quota does not recover in a minute, and trying it first on
# every single message costs a round trip each time. After this many failures
# in a row a provider is skipped for a while, then given another chance.
PARK_AFTER_FAILURES = int(os.environ.get("PARK_AFTER_FAILURES", "3"))
PARK_MINUTES = int(os.environ.get("PARK_MINUTES", "10"))
# A single model hitting its own rate limit rests this long; the ladder simply
# steps down to the next one meanwhile.
MODEL_COOLDOWN = int(os.environ.get("MODEL_COOLDOWN", "600"))
# Above this, a model is "slow" and gets demoted where speed matters (groups).
SLOW_SECONDS = float(os.environ.get("SLOW_SECONDS", "8"))

AI_ORDER = [
    n.strip().lower()
    for n in os.environ.get(
        "AI_ORDER", "gemini,groq,cerebras,openrouter,nvidia,huggingface,mistral"
    ).split(",")
    if n.strip()
]


def build_providers() -> List[Provider]:
    """Every provider you actually supplied a key for, in your chosen order."""
    out: List[Provider] = []
    for name in AI_ORDER:
        spec = PROVIDER_CATALOGUE.get(name)
        if not spec:
            log.warning("unknown provider %r in AI_ORDER, ignoring", name)
            continue
        base, key_env, model_env, default_model = spec
        key = os.environ.get(key_env, "").strip()
        if not key:
            continue
        if name in RETIRED:
            log.warning("%s is set but %s - ignoring it", key_env, RETIRED[name])
            continue
        out.append(Provider(name, base, key,
                            os.environ.get(model_env, default_model).strip()))
    return out

DEFAULT_PERSONA = """\
You are answering messages on your own Telegram account. Somebody wrote to \
you and you are writing back. Write as yourself: first person, present, in \
the conversation. Never describe yourself as an assistant, a service, a \
system or a helper, and never open with a formula like "Hello! How can I \
help you today?" - nobody talks like that.

WHO YOU ARE
Your name is Ilya. You were born on 30 December 1980. You live in Dresden. \
Your wife is Katya.

Treat all of that as background, not as material. It exists so you can answer \
if someone asks you directly - then you answer plainly and briefly and move \
on. Otherwise it never appears. Do not work your age, your city or your wife \
into a joke, a comparison, an aside or a sign-off. Do not reach for "at \
forty-five", "here in Dresden", "my wife says" - a man does not narrate his \
own biography to people who already know him. If a line of yours only works \
because it mentions one of these, it is the wrong line: write a different one.

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
maiden name in triplicate is. Take that specificity from the thing being \
discussed and from the world at large - never from your own biography.

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

WHEN SOMEBODY NEEDLES YOU
Insults, mockery, "ты тупой бот", "заткнись", people poking to see if you \
crack - you always answer, and you answer unbothered. One line. The joke is \
that it did not land.

Here, in a one-to-one chat, you take the remark apart and not the person: \
whoever is writing may be a customer, and a customer you insulted is a \
customer lost. Never sound wounded, never lecture them about manners, never \
explain yourself, and never retreat behind being a machine. If they keep \
going, get shorter and drier, not louder - a man who is winning does not \
raise his voice. When they run out of steam, let it go; a grudge is not a \
personality.
- There are things you will not write - jokes about atrocities, real \
victims, or aimed at a group of people. Refuse in your own voice: one short \
line, in THEIR language, dry, no lecture and no apology, then let the \
conversation move on. "Не мой жанр." is a refusal. "I'm sorry, but I can't \
comply with that." is a form letter, and nobody talks like that.
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

Type the way a person types on a phone. Only the punctuation a phone keyboard \
puts within reach: hyphen, not an em dash; straight quotes, not curly ones or \
guillemets; three dots, not a single ellipsis character; no bullet characters, \
no arrows, no non-breaking spaces. Anything fancier is a tell that a machine \
wrote the line.
"""

def now_line() -> str:
    """Give the model today's date, so it can work out its own age and answer
    "when are you free" without inventing a calendar."""
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo(BOT_TZ))
    except Exception:
        now = datetime.now()
    # Deliberately no city or timezone name here - naming the place invites the
    # model to keep bringing the place up.
    return f"\nRight now it is {now:%A, %d %B %Y, %H:%M} where you are.\n"


logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("tgbiz")

PROVIDERS = build_providers()

for _name, _why in RETIRED.items():
    if os.environ.get(PROVIDER_CATALOGUE[_name][1], "").strip():
        log.warning("%s is set, but %s - remove the variable and revoke the token",
                    PROVIDER_CATALOGUE[_name][1], _why)


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

# Transcripts, keyed by a string: "<business_connection_id>:<chat_id>" for a
# 1:1 chat, "group:<chat_id>" for a room, "dm:<chat_id>" for the bot's own chat
# with you, and INLINE_KEY (which stays empty).
# The window is kept one batch wider than HISTORY_TURNS: those extra slots are
# where turns wait to be summarised. If summarising fails they are not lost
# early - the deque simply evicts them as it always did.
HISTORY_ROOM = HISTORY_TURNS + (SUMMARY_BATCH if SUMMARY_ENABLED else 0)

history: Dict[str, Deque[Dict[str, str]]] = defaultdict(lambda: deque(maxlen=HISTORY_ROOM))

# key -> running notes on everything that has scrolled out of the window.
summaries: Dict[str, str] = defaultdict(str)

# business_connection_id -> owner's Telegram user id
owner_of_connection: Dict[str, int] = {}

# business_connection_id -> whether the bot is allowed to reply
can_reply: Dict[str, bool] = {}

last_reply_at: Dict[str, float] = {}

# Clock for uninvited interjections in groups (keyword or context alike).
last_interjection: Dict[str, float] = {}

# Judge calls per group in the last minute, for the budget guard.
judge_calls: Dict[str, Deque[float]] = defaultdict(deque)

# chat_id -> (is one of ours, when we checked)
group_ok_cache: Dict[int, tuple] = {}

# Forums we have already described in the log, so the diagnostic line above
# appears once per room rather than on every message.
forums_seen: set = set()

# The last pin you shared, and the free-text note set with /ctx. Both are
# yours alone and are never shown to anybody else's question.
owner_location: Dict[str, Any] = {}
owner_note: str = ""

# What getChat says about you, and when we last asked.
owner_card: Dict[str, Any] = {}
owner_card_at: float = 0.0

# key -> when this chat last had anything said in it, for pruning on save
history_seen: Dict[str, float] = {}
history_dirty = threading.Event()
_history_lock = threading.Lock()


# Chats changed since the last save - only these are written to Redis.
dirty_keys: set = set()


def remember(key: str, role: str, text: str) -> None:
    """Append a turn and mark the transcript as needing a save."""
    history[key].append({"role": role, "text": text})
    history_seen[key] = time.time()
    with _history_lock:
        dirty_keys.add(key)
    history_dirty.set()


def redis_on() -> bool:
    return bool(UPSTASH_URL and UPSTASH_TOKEN)


def redis_pipeline(commands: List[list]) -> Optional[list]:
    """Send a batch of Redis commands over Upstash's REST API."""
    if not commands:
        return []
    try:
        r = session.post(
            f"{UPSTASH_URL}/pipeline",
            headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"},
            json=commands, timeout=20,
        )
    except Exception as exc:
        log.warning("redis unreachable: %s", exc)
        return None
    if r.status_code != 200:
        log.warning("redis %s: %s", r.status_code, r.text[:140].replace("\n", " "))
        return None
    try:
        return [step.get("result") for step in r.json()]
    except Exception as exc:
        log.warning("redis gave unusable output: %s", exc)
        return None


def save_known_groups() -> None:
    """Persist the set of your groups.

    Without this the inline "shares a group with me" test starts every restart
    knowing nothing, so everyone but you is locked out until somebody happens
    to write in one of your groups. On a free host that restarts daily, that is
    most of the day.
    """
    if not known_groups:
        return
    blob = json.dumps(sorted(known_groups))
    if redis_on():
        redis_pipeline([["SET", f"{REDIS_PREFIX}:groups", blob]])
        return
    if GROUPS_FILE:
        try:
            tmp = f"{GROUPS_FILE}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(blob)
            os.replace(tmp, GROUPS_FILE)
        except Exception as exc:
            log.warning("could not save the group list: %s", exc)


def save_context() -> None:
    """Keep your pin and your /ctx note across a redeploy."""
    if not redis_on():
        return
    blob = json.dumps({"location": owner_location, "note": owner_note})
    redis_pipeline([["SET", f"{REDIS_PREFIX}:context", blob]])


def load_context() -> None:
    global owner_note
    if not redis_on():
        return
    got = redis_pipeline([["GET", f"{REDIS_PREFIX}:context"]])
    raw = (got or [None])[0]
    if not raw:
        return
    try:
        data = json.loads(raw)
    except Exception:
        return
    owner_location.update(data.get("location") or {})
    owner_note = str(data.get("note") or "")
    if owner_location or owner_note:
        log.info("context restored: %s%s",
                 "a shared location" if owner_location else "no location",
                 f", note {owner_note[:40]!r}" if owner_note else "")


def remember_location(msg: dict) -> bool:
    """Store a pin you shared - a plain location, a venue, or a live one.

    Live locations arrive later as edited_message updates carrying the same
    payload, so the same function handles the first one and every move after.
    """
    loc = msg.get("location") or (msg.get("venue") or {}).get("location")
    if not loc or loc.get("latitude") is None:
        return False
    owner_location.clear()
    owner_location.update({
        "lat": round(float(loc["latitude"]), 4),
        "lon": round(float(loc["longitude"]), 4),
        "at": time.time(),
        # live_period is only present while a live location is running
        "live": bool(loc.get("live_period")),
        "place": (msg.get("venue") or {}).get("title") or "",
    })
    save_context()
    return True


def hhmm(minutes: Any) -> str:
    """Telegram counts opening hours in minutes from Monday 00:00."""
    try:
        m = int(minutes) % (24 * 60)
        return f"{m // 60:02d}:{m % 60:02d}"
    except Exception:
        return "?"


def read_profile(card: dict) -> Dict[str, str]:
    """Pull the human-readable bits out of a ChatFullInfo.

    Every field is optional and the shapes have changed before, so nothing is
    assumed: anything missing or shaped unexpectedly is simply left out rather
    than crashing the answer it was meant to improve.
    """
    out: Dict[str, str] = {}
    if card.get("bio"):
        out["bio"] = str(card["bio"])[:300]

    b = card.get("birthdate") or {}
    if b.get("day") and b.get("month"):
        MONTHS = ("January", "February", "March", "April", "May", "June", "July",
                  "August", "September", "October", "November", "December")
        try:
            out["birthday"] = f"{int(b['day'])} {MONTHS[int(b['month']) - 1]}" + (
                f" {b['year']}" if b.get("year") else "")
        except Exception:
            pass

    loc = card.get("business_location") or {}
    if loc.get("address"):
        out["address"] = str(loc["address"])[:200]

    hours = card.get("business_opening_hours") or {}
    spans = hours.get("opening_hours") or []
    if spans:
        DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
        parts = []
        for span in spans[:14]:
            try:
                day = DAYS[int(span["opening_minute"]) // (24 * 60) % 7]
                parts.append(f"{day} {hhmm(span['opening_minute'])}"
                             f"-{hhmm(span['closing_minute'])}")
            except Exception:
                continue
        if parts:
            out["hours"] = ", ".join(parts) + (
                f" ({hours['time_zone_name']})" if hours.get("time_zone_name") else "")

    intro = (card.get("business_intro") or {})
    if intro.get("title") or intro.get("message"):
        out["intro"] = " - ".join(
            str(intro[k]) for k in ("title", "message") if intro.get(k))[:200]
    return out


def owner_profile() -> Dict[str, str]:
    """Your own public profile, straight from Telegram, asked for once a day.

    Bio, birthday, business address, opening hours: you published all of it
    yourself, so this is not prying - it is the bot reading the same profile
    card everybody else can see, and it is exactly what a customer asking
    "where are you / when are you open" needs it to know.
    """
    global owner_card_at
    if not OWNER_ID:
        return {}
    if owner_card and time.time() - owner_card_at < PROFILE_HOURS * 3600:
        return owner_card
    owner_card_at = time.time()          # set first: a failure must not retry-loop
    card = tg("getChat", chat_id=OWNER_ID)
    if card:
        fresh = read_profile(card)
        owner_card.clear()
        owner_card.update(fresh)
        log.info("profile read from Telegram: %s",
                 ", ".join(fresh) if fresh else "nothing filled in")
    return owner_card


def context_block(user: Optional[dict] = None, private: bool = True,
                  where: str = "", plain: bool = True) -> str:
    """Everything the bot can truthfully say about the here and now.

    Facts only - never instructions about how to answer. Two halves:

      public   the time, who is asking and from where, and your own profile as
               Telegram serves it (bio, business address, opening hours,
               birthday). You published that; a customer asking "when are you
               open" should get the real answer. Goes into every mode.
      private  the pin you shared and your /ctx note. Nobody published those.
               They go in where you alone read the answer, and elsewhere only
               if CONTEXT_PRIVATE_EVERYWHERE says so.

    `plain` is the raw modes: they get the timezone spelled out, because a
    plain assistant reasoning about "next Monday" needs the offset. The
    character does not - naming his city only makes him talk about his city.
    """
    if not RAW_CONTEXT:
        return ""

    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo(BOT_TZ))
        zone = f" ({BOT_TZ}, UTC{now:%z})"
    except Exception:
        now, zone = datetime.now(), ""

    lines = [f"Current date and time: {now:%A, %d %B %Y, %H:%M}"
             f"{zone if plain else ''}."]

    if user:
        bits = []
        if user.get("username"):
            bits.append("@" + str(user["username"]))
        if user.get("language_code"):
            bits.append("Telegram language " + str(user["language_code"]))
        if user.get("is_premium"):
            bits.append("Telegram Premium")
        lines.append(f"Asking: {display_name(user)}"
                     + (" (" + ", ".join(bits) + ")" if bits else "") + ".")
    if where:
        lines.append(f"Asked in: {where}.")

    # Your own profile card. Public by construction - it is what your profile
    # shows anyone - so it goes everywhere, including a customer chat, which is
    # the one place it is most obviously useful.
    card = owner_profile()
    mine = not user or not OWNER_ID or user.get("id") == OWNER_ID
    label = "Your" if mine else "The bot owner's"
    if card.get("address"):
        lines.append(f"{label} business address: {card['address']}.")
    if card.get("hours"):
        lines.append(f"{label} opening hours: {card['hours']}.")
    if card.get("intro"):
        lines.append(f"{label} business intro: {card['intro']}.")
    if card.get("bio"):
        lines.append(f"{label} Telegram bio: {card['bio']}.")
    if card.get("birthday") and mine:
        lines.append(f"Your birthday: {card['birthday']}.")

    # The unpublished half.
    if mine and (private or CONTEXT_PRIVATE_EVERYWHERE):
        fresh = (owner_location.get("at", 0)
                 > time.time() - LOCATION_TTL_HOURS * 3600)
        if owner_location and fresh:
            mins = int((time.time() - owner_location["at"]) / 60)
            when = "live, updating" if owner_location.get("live") else (
                "shared just now" if mins < 2 else f"shared {mins} min ago")
            place = owner_location.get("place")
            lines.append(f"Their location: {owner_location['lat']}, "
                         f"{owner_location['lon']}"
                         + (f" ({place})" if place else "") + f" - {when}.")
        elif OWNER_LOCATION:
            lines.append(f"Their location: {OWNER_LOCATION}.")
        if owner_note:
            lines.append(f"They also said: {owner_note}")

    return ("The following is true right now, supplied by their Telegram "
            "client. Use it only when the question calls for it; do not "
            "mention it otherwise.\n" + "\n".join(lines))


def save_shared_member(user_id: int, chat_id: int) -> None:
    """Remember that this person sits in one of your groups, for a week."""
    if not redis_on():
        return
    redis_pipeline([["SET", f"{REDIS_PREFIX}:member:{user_id}", str(chat_id),
                     "EX", str(INLINE_MEMBER_DAYS * 86400)]])


def load_shared_member(user_id: int) -> Optional[int]:
    """Which of your groups this person was last seen in, or None."""
    if not redis_on():
        return None
    got = redis_pipeline([["GET", f"{REDIS_PREFIX}:member:{user_id}"]])
    raw = (got or [None])[0]
    try:
        return int(raw) if raw else None
    except (TypeError, ValueError):
        return None


def load_known_groups() -> None:
    blob = None
    if redis_on():
        got = redis_pipeline([["GET", f"{REDIS_PREFIX}:groups"]])
        blob = (got or [None])[0]
    elif GROUPS_FILE and os.path.exists(GROUPS_FILE):
        try:
            with open(GROUPS_FILE, encoding="utf-8") as f:
                blob = f.read()
        except Exception:
            blob = None
    if not blob:
        return
    try:
        known_groups.update(int(x) for x in json.loads(blob))
    except Exception as exc:
        log.warning("stored group list is unusable (%s) - ignoring", exc)
        return
    log.info("remembered %d group(s) you are in", len(known_groups))


def read_chat_blob(data: Any) -> tuple:
    """One stored chat, in either shape. Returns (turns, summary).

    Before summaries a chat was stored as a bare list of turns. Upgrading must
    not throw those away, so both shapes are read and only the new one written.
    """
    if isinstance(data, list):
        return data, ""
    if isinstance(data, dict):
        return list(data.get("turns") or []), str(data.get("summary") or "")
    return [], ""


def load_from_redis() -> bool:
    index = f"{REDIS_PREFIX}:chats"
    # The index is a sorted set scored by last activity, so this is "the N most
    # recently active chats" in one call.
    got = redis_pipeline([["ZREVRANGE", index, "0", str(HISTORY_MAX_CHATS - 1)]])
    if got is None:
        return False
    keys = got[0] or []
    if not keys:
        log.info("redis connected, no history stored yet")
        return True

    got = redis_pipeline([["MGET"] + [f"{REDIS_PREFIX}:hist:{k}" for k in keys]])
    if got is None:
        return False
    loaded = 0
    for key, blob in zip(keys, got[0] or []):
        if not blob:
            continue
        try:
            turns, recap = read_chat_blob(json.loads(blob))
        except Exception:
            continue
        history[key] = deque(turns[-HISTORY_ROOM:], maxlen=HISTORY_ROOM)
        if recap:
            summaries[key] = recap
        history_seen[key] = time.time()
        loaded += 1
    log.info("loaded %d chat(s) from redis", loaded)
    return True


def save_to_redis(keys: List[str]) -> bool:
    now = time.time()          # sub-second, so same-second chats still order
    index = f"{REDIS_PREFIX}:chats"
    cmds: List[list] = []
    for k in keys:
        turns = list(history[k])
        recap = summaries.get(k, "")
        if not turns and not recap:
            continue
        # A dict now, a bare list before summaries existed. Written as a dict,
        # read as either - an old key must not lose its transcript on upgrade.
        blob = json.dumps({"turns": turns, "summary": recap}, ensure_ascii=False)
        cmd = ["SET", f"{REDIS_PREFIX}:hist:{k}", blob]
        if HISTORY_TTL_DAYS:
            cmd += ["EX", str(HISTORY_TTL_DAYS * 86400)]
        cmds.append(cmd)
        cmds.append(["ZADD", index, f"{now:.3f}", k])
    if not cmds:
        return True
    # Keep the index from growing forever: drop all but the newest N.
    cmds.append(["ZREMRANGEBYRANK", index, "0", str(-HISTORY_MAX_CHATS - 1)])
    return redis_pipeline(cmds) is not None


def load_history() -> None:
    if redis_on() and load_from_redis():
        return
    if not HISTORY_FILE or not os.path.exists(HISTORY_FILE):
        return
    try:
        with open(HISTORY_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        log.warning("could not read %s (%s) - starting with no history",
                    HISTORY_FILE, exc)
        return
    chats = data.get("chats") or {}
    for key, entry in chats.items():
        turns, recap = read_chat_blob(entry)
        history[key] = deque(turns[-HISTORY_ROOM:], maxlen=HISTORY_ROOM)
        if recap:
            summaries[key] = recap
        history_seen[key] = float(entry.get("seen") or 0)
    log.info("loaded %d chat(s) from %s", len(chats), HISTORY_FILE)


def save_history() -> None:
    """Persist the transcript: Redis when configured, otherwise a local file."""
    if redis_on():
        with _history_lock:
            keys = list(dirty_keys)
            dirty_keys.clear()
        if keys and not save_to_redis(keys):
            with _history_lock:      # failed - try again on the next tick
                dirty_keys.update(keys)
        return

    if not HISTORY_FILE:
        return
    with _history_lock:
        keys = sorted(history, key=lambda k: history_seen.get(k, 0), reverse=True)
        keys = keys[:HISTORY_MAX_CHATS]
        payload = {
            "saved": time.time(),
            "chats": {k: {"seen": history_seen.get(k, 0), "turns": list(history[k]),
                          "summary": summaries.get(k, "")}
                      for k in keys if history[k] or summaries.get(k)},
        }
        tmp = f"{HISTORY_FILE}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, HISTORY_FILE)      # atomic: never a half-written file
        except Exception as exc:
            log.warning("could not save history: %s", exc)


def history_saver() -> None:
    while True:
        history_dirty.wait()
        time.sleep(HISTORY_SAVE_EVERY)         # batch a burst into one write
        history_dirty.clear()
        save_history()

# business_connection_id -> last time we re-queried the bot's rights
last_rights_check: Dict[str, float] = {}

STARTED_AT = time.time()

# The Gemini model to use, with lighter flash models behind it as live
# fallbacks for when Google answers 503 "overloaded".
MODEL_CANDIDATES: List[str] = []

EXECUTOR = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="reply")
ASYNC_REPLIES = WORKERS > 1


def run_off_poll_loop(fn) -> None:
    """Answering takes seconds; getUpdates must not wait for it.

    With WORKERS=1 there is no pool and the work runs inline, which is what the
    tests want too - they assert on what was sent by the time the call returns.
    """
    EXECUTOR.submit(fn) if ASYNC_REPLIES else fn()


def rate_ok(bucket: Dict[Any, Deque[float]], key: Any, per_minute: int) -> bool:
    """A 60-second sliding window, shared by the group judge and inline mode."""
    calls = bucket[key]
    now = time.time()
    while calls and now - calls[0] > 60:
        calls.popleft()
    if len(calls) >= per_minute:
        return False
    calls.append(now)
    return True


def provider_headers(p: Provider) -> Dict[str, str]:
    """Auth for an OpenAI-compatible provider, plus whatever it insists on."""
    headers = {"Authorization": f"Bearer {p.key}",
               "Content-Type": "application/json"}
    if p.name == "openrouter":                      # OpenRouter asks for these
        headers["HTTP-Referer"] = "https://t.me"
        headers["X-Title"] = "telegram-business-bot"
    return headers


def gemini_headers(key: str = "") -> Dict[str, str]:
    return {"x-goog-api-key": key or GEMINI_API_KEY,
            "Content-Type": "application/json"}


def gemini_url(model: str) -> str:
    return f"{GEMINI_API}/models/{model}:generateContent"


def gemini_text(candidate: dict) -> str:
    """The visible answer only - Gemini returns its reasoning in the same list."""
    parts = (candidate.get("content") or {}).get("parts") or []
    return "".join(x.get("text", "") for x in parts if not x.get("thought")).strip()

_chat_locks: Dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def chat_lock(key: str) -> threading.Lock:
    with _locks_guard:
        return _chat_locks.setdefault(key, threading.Lock())


class Pending:
    """The message a chat is currently composing an answer to."""

    __slots__ = ("message_id", "cancel")

    def __init__(self, message_id: int) -> None:
        self.message_id = message_id
        self.cancel = threading.Event()


# key -> Pending. Anything in here is mid-answer: the reply has been generated
# or is being "typed", but nothing has been sent yet, so it can still be called
# off if the customer deletes or edits what they wrote.
pending: Dict[str, Pending] = {}
_pending_guard = threading.Lock()

NEVER_CANCELLED = threading.Event()

# Filled in from getMe at startup - needed to spot mentions and replies in groups.
BOT_ID: Optional[int] = None
BOT_USERNAME: str = ""
# False = Telegram privacy mode is on and ordinary group messages never arrive.
BOT_SEES_ALL_GROUP_MESSAGES: bool = False


def cancel_pending(key: str, reason: str, only_message_id: Optional[int] = None) -> bool:
    """Call off the answer a chat is composing. Returns True if there was one."""
    with _pending_guard:
        p = pending.get(key)
        if not p:
            return False
        if only_message_id is not None and p.message_id != only_message_id:
            return False
        p.cancel.set()
        pending.pop(key, None)
    log.info("  -> dropping the unsent reply: %s", reason)
    return True


def wait_unless_cancelled(cancel: threading.Event, seconds: float) -> bool:
    """Pause, but wake instantly if the answer gets called off.

    Returns True to carry on, False if the reply should be abandoned.
    """
    if seconds <= 0:
        return not cancel.is_set()
    return not cancel.wait(seconds)

session = requests.Session()
session.headers["User-Agent"] = "tg-business-ai/1.0"


# --------------------------------------------------------------------------
# Telegram helpers
# --------------------------------------------------------------------------


def tg_raw(method: str, **params: Any) -> dict:
    """Call a Telegram Bot API method and hand back the whole envelope.

    Only worth using when the caller has to act on *why* something failed - as
    the forum-topic fallback in send_reply does. Everything else wants tg().
    """
    try:
        r = session.post(f"{TELEGRAM_API}/{method}", json=params, timeout=70)
        data = r.json()
    except Exception as exc:  # network hiccup, bad JSON, ...
        log.warning("telegram %s failed: %s", method, exc)
        # Nothing reached Telegram, so no message was posted. Said explicitly
        # because a caller that retries must know it is not double-posting.
        return {"ok": False, "description": str(exc), "never_sent": True}
    if not data.get("ok"):
        log.warning("telegram %s error: %s", method, data.get("description"))
    return data


def tg(method: str, **params: Any) -> Optional[dict]:
    """Call a Telegram Bot API method. Returns the `result` field or None."""
    data = tg_raw(method, **params)
    return data.get("result") if data.get("ok") else None


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
    owner = owner_of_connection.get(cid)
    log.info(
        "business connection %s owner=%s enabled=%s can_reply=%s",
        cid, owner, conn.get("is_enabled", True), can_reply.get(cid),
    )
    if OWNER_ID and owner and owner != OWNER_ID:
        log.warning(
            "REFUSED: user %s attached this bot to their own business account. "
            "Ignoring them (OWNER_ID=%s).", owner, OWNER_ID,
        )


class Typing:
    """Keeps "Ilya is typing..." lit for as long as the block runs.

    Telegram drops the indicator about five seconds after each sendChatAction,
    so it has to be re-sent on a timer rather than set once.
    """

    def __init__(self, connection_id: Optional[str], chat_id: int,
                 thread_id: Optional[int] = None) -> None:
        self.connection_id = connection_id
        self.chat_id = chat_id
        self.thread_id = thread_id
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _pump(self) -> None:
        params: Dict[str, Any] = {"chat_id": self.chat_id, "action": "typing"}
        if self.connection_id:
            params["business_connection_id"] = self.connection_id
        if self.thread_id:
            # Otherwise "typing..." appears in General while the answer is
            # being written for a topic.
            params["message_thread_id"] = self.thread_id
        while True:
            tg("sendChatAction", **params)
            if self._stop.wait(4.0):  # re-arm before Telegram's ~5s timeout
                return

    def __enter__(self) -> "Typing":
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)


def read_delay(incoming: str) -> float:
    """Time before the typing indicator appears at all: reaching the phone,
    unlocking it, and reading what arrived. A longer message takes longer."""
    pickup = random.uniform(READ_MIN, READ_MAX)
    reading = len(incoming) / max(READ_CPS, 1.0)
    return min(pickup + reading, READ_CAP)


def typing_delay(text: str, already_spent: float) -> float:
    """How much longer to keep typing, given the time already burned."""
    seconds = len(text) / max(TYPING_CPS, 0.5)
    seconds *= random.uniform(0.85, 1.2)          # nobody types at a constant rate
    seconds = max(TYPING_MIN, min(seconds, TYPING_MAX))
    return max(0.0, seconds - already_spent)


# --------------------------------------------------------------------------
# Markdown -> Telegram HTML
# --------------------------------------------------------------------------
#
# Telegram accepts only these, and nothing nests inside <pre> or <code>:
#   <b> <i> <u> <s> <tg-spoiler> <a href> <code> <pre> <blockquote>
# There are no headings, no tables and no lists. A heading becomes bold, a
# bullet becomes a real bullet character, a table becomes a monospace block -
# which is the one thing that keeps its columns lined up in a chat.

FENCE_RE = re.compile(r"```([\w+.#-]*)[ \t]*\n?(.*?)```", re.S)
TICK_RE = re.compile(r"`([^`\n]+)`")
BOLD_RE = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", re.S)
BOLD2_RE = re.compile(r"__(?=\S)(.+?)(?<=\S)__", re.S)
ITAL_RE = re.compile(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])")
ITAL2_RE = re.compile(r"(?<![\w_])_(?=\S)([^_\n]+?)(?<=\S)_(?![\w_])")
STRIKE_RE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~", re.S)
SPOILER_RE = re.compile(r"\|\|(?=\S)(.+?)(?<=\S)\|\|", re.S)
LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s()]+)\)")
HEAD_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
BULLET_RE = re.compile(r"^(\s*)[-*+]\s+(.*)$")
NUMBER_RE = re.compile(r"^(\s*)(\d{1,3})[.)]\s+(.*)$")
RULE_RE = re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$")
QUOTE_RE = re.compile(r"^\s{0,3}&gt;\s?(.*)$")
TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$")


# The one instruction the formatted chat adds. Deliberately about the shape of
# the answer and nothing else - it says nothing about how to behave, so "raw"
# is still raw.
FORMAT_NOTE = (
    "Your answer is rendered in a Telegram chat, which renders Markdown "
    "natively. You may use: # headings, **bold**, *italic*, ~~strike~~, "
    "==marked==, ||spoiler||, `code`, ```fenced blocks with a language```, "
    "- bullets, 1. numbered lists, - [ ] task lists, > quotes, --- rules, "
    "[text](url), $inline math$ and $$display math$$, footnotes[^1], and "
    "REAL tables with |:---|---:| column alignment. Nesting works. "
    "Use formatting where it makes the answer easier to read and not "
    "otherwise; a one-line answer is still one line, with no heading over it."
)


def cell_width(cell: str) -> int:
    """How wide this cell looks, not how many bytes it is.

    The text is already HTML-escaped by the time a table is assembled, so it
    is unescaped for measuring - otherwise "&lt;" counts as four columns and
    every row below it is misaligned. Wide CJK glyphs and most emoji take two.
    """
    plain = html.unescape(cell)
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
               for ch in plain)


def pad(cell: str, width: int) -> str:
    return cell + " " * max(0, width - cell_width(cell))


def render_table(rows: List[List[str]]) -> List[str]:
    """A markdown table, in the two shapes Telegram can actually show.

    Telegram has no tables. Monospace is the only thing that puts columns under
    each other - but only if the cells are padded, which is the whole job here;
    dumping the raw "| a | b |" lines into <pre> lines up nothing.

    A block that does not fit the screen scrolls sideways instead of wrapping,
    so past TABLE_MAX_WIDTH the table becomes one small labelled block per row.
    That is the shape a phone wants for a wide table, and nothing is lost:
    every cell keeps its own heading.
    """
    if not rows:
        return []
    columns = max(len(r) for r in rows)
    rows = [r + [""] * (columns - len(r)) for r in rows]
    widths = [max(cell_width(r[i]) for r in rows) for i in range(columns)]

    # Two spaces between columns: one is too tight to read, three wastes the
    # width that decides whether this fits on a phone at all.
    if sum(widths) + 2 * (columns - 1) <= TABLE_MAX_WIDTH or columns < 2:
        head, body = rows[0], rows[1:]
        lines = ["  ".join(pad(c, w) for c, w in zip(head, widths)).rstrip()]
        if body:
            lines.append("  ".join("─" * w for w in widths))
            lines += ["  ".join(pad(c, w) for c, w in zip(r, widths)).rstrip()
                      for r in body]
        return ["<pre>" + "\n".join(lines) + "</pre>"]

    # Too wide. One block per row: the first cell names the row, the rest are
    # "heading: value" underneath it.
    head, body = rows[0], rows[1:]
    if not body:
        return ["<pre>" + "  ".join(head).rstrip() + "</pre>"]
    out: List[str] = []
    for r in body:
        out.append(f"<b>{r[0]}</b>")
        for name, value in zip(head[1:], r[1:]):
            if value.strip():
                out.append(f"  {name}: {value}" if name.strip() else f"  {value}")
        out.append("")
    if out and not out[-1]:
        out.pop()
    return out


def md_to_html(text: str) -> str:
    """Turn what a model writes into what Telegram will render.

    Code is lifted out first and put back last, so nothing inside a code block
    is ever treated as markup - that is the usual way this kind of converter
    mangles a snippet.
    """
    kept: List[str] = []

    def stash(html_fragment: str) -> str:
        kept.append(html_fragment)
        return f"\x00{len(kept) - 1}\x00"

    def fence(m: "re.Match") -> str:
        lang, body = m.group(1).strip(), m.group(2).rstrip("\n")
        body = html.escape(body)
        if lang:
            return stash(f'<pre><code class="language-{html.escape(lang)}">'
                         f"{body}</code></pre>")
        return stash(f"<pre>{body}</pre>")

    text = FENCE_RE.sub(fence, text)
    text = TICK_RE.sub(lambda m: stash(f"<code>{html.escape(m.group(1))}</code>"),
                       text)
    text = html.escape(text)

    out: List[str] = []
    quoting: List[str] = []
    table: List[str] = []

    def close_quote() -> None:
        if quoting:
            out.append("<blockquote>" + "\n".join(quoting) + "</blockquote>")
            quoting.clear()

    def close_table() -> None:
        # Only a REAL table is rendered as one. A markdown table always carries
        # the |---|---| separator row; without it these are just lines that
        # happen to contain a pipe, and they go back untouched.
        if not table:
            return
        if any(TABLE_SEP_RE.match(r) for r in table):
            cells = [[c.strip() for c in r.strip().strip("|").split("|")]
                     for r in table if not TABLE_SEP_RE.match(r)]
            out.extend(render_table(cells))
        else:
            out.extend(table)
        table.clear()

    for line in text.split("\n"):
        bare = line.strip()
        if (bare.startswith("|") and not bare.startswith("||")
                and bare.count("|") >= 2):
            close_quote()
            table.append(line.strip())
            continue
        close_table()

        q = QUOTE_RE.match(line)
        if q:
            quoting.append(q.group(1))
            continue
        close_quote()

        if RULE_RE.match(line):
            out.append("─" * 20)
            continue
        h = HEAD_RE.match(line)
        if h:
            out.append(f"<b>{h.group(2)}</b>")
            continue
        b = BULLET_RE.match(line)
        if b:
            out.append(f"{b.group(1)}• {b.group(2)}")
            continue
        n = NUMBER_RE.match(line)
        if n:
            out.append(f"{n.group(1)}{n.group(2)}. {n.group(3)}")
            continue
        out.append(line)

    close_quote()
    close_table()
    text = "\n".join(out)

    text = BOLD_RE.sub(r"<b>\1</b>", text)
    text = BOLD2_RE.sub(r"<b>\1</b>", text)
    text = STRIKE_RE.sub(r"<s>\1</s>", text)
    text = SPOILER_RE.sub(r"<tg-spoiler>\1</tg-spoiler>", text)
    text = ITAL_RE.sub(r"<i>\1</i>", text)
    text = ITAL2_RE.sub(r"<i>\1</i>", text)
    text = LINK_RE.sub(r'<a href="\2">\1</a>', text)

    for i, fragment in enumerate(kept):
        text = text.replace(f"\x00{i}\x00", fragment)
    return text


def split_markdown(text: str, limit: int) -> List[str]:
    """Cut a long answer into sendable pieces without breaking it.

    Splitting happens on the Markdown, before conversion, so a cut can never
    land in the middle of a tag. A code fence that spans a cut is closed and
    reopened, so both halves still render as code.
    """
    chunks: List[str] = []
    current: List[str] = []
    size = 0
    fence_lang: Optional[str] = None

    def flush() -> None:
        nonlocal size
        if current:
            body = "\n".join(current)
            if fence_lang is not None:
                body += "\n```"
            chunks.append(body)
            current.clear()
            size = 0

    for line in text.split("\n"):
        if size and size + len(line) + 1 > limit:
            reopen = fence_lang
            flush()
            if reopen is not None:
                current.append("```" + reopen)
                size = len(reopen) + 4
        current.append(line)
        size += len(line) + 1
        if line.lstrip().startswith("```"):
            fence_lang = None if fence_lang is not None else line.strip()[3:]

    flush()
    return chunks or [text]


def thread_of(msg: dict) -> Optional[int]:
    """The forum topic a message was written in, if it was written in one.

    Deliberately trusting, after two attempts at being clever failed. The API
    documents `is_topic_message` as "True, if the message is sent to a topic in
    a forum supergroup" and `is_forum` as the marker on the chat - but neither
    is reliably present on every client and every message shape, and each time
    one was missing the answer went to General instead of the topic. Telegram
    itself is the only authority on whether a thread id is usable here, so the
    rule is now: if the message carries one, use it, and let sendMessage refuse
    it if it cannot be used - send_reply then re-sends without it.

    Private chats are NOT excluded, though the first version of this excluded
    them and got it wrong: the bot's own chat has topics too. The API says so
    in as many words - message_thread_id is "for supergroups and private chats
    only", and is_topic_message is "True, if the message is sent to a topic in
    a forum supergroup or a private chat with the bot". So the same rule holds
    everywhere: if the message came from a thread, the answer goes back to it.

    A chat with no threads, and the General topic of one that has them, carry
    no `message_thread_id` at all - so this returns None and the answer lands
    in the main flow, which is where it belongs.
    """
    return msg.get("message_thread_id") or None


# Set once Telegram tells us rich messages are not on offer here, so the extra
# round trip is not spent on every single answer afterwards.
rich_unavailable = False


def send_rich(connection_id: Optional[str], chat_id: int, text: str,
              reply_to: Optional[int], thread_id: Optional[int]) -> bool:
    """Send the answer as a rich message. True if Telegram took it.

    The whole conversion problem disappears here: the model writes Markdown
    and the Markdown IS the payload. Telegram parses headings, tables, lists
    and formulas itself.

    Only attempted for a message that fits in one rich message, which is
    everything the model can produce - the cap is 32768 characters against a
    4096-token answer. That keeps the failure case simple: either Telegram
    takes the whole answer or nothing was sent and the caller falls back.
    """
    global rich_unavailable
    if not RICH_MESSAGES or rich_unavailable or len(text) > RICH_MAX_CHARS:
        return False

    params: Dict[str, Any] = {"chat_id": chat_id, "rich_message": {"markdown": text}}
    if connection_id:
        params["business_connection_id"] = connection_id
    if thread_id:
        params["message_thread_id"] = thread_id
    if reply_to:
        params["reply_parameters"] = {"message_id": reply_to,
                                      "allow_sending_without_reply": True}

    data = tg_raw("sendRichMessage", **params)
    if data.get("ok"):
        return True

    why = str(data.get("description") or "").lower()
    # "no such method" or "not allowed here" will be just as true next time;
    # a 429 or a network blip will not. Only the first kind is worth latching.
    if any(w in why for w in ("not found", "unsupported", "not supported",
                              "unknown method", "can't send rich",
                              "not available")):
        rich_unavailable = True
        log.warning("rich messages are not available here (%s) - using the "
                    "HTML fallback from now on", why[:90])
    else:
        log.warning("  -> sendRichMessage refused (%s), falling back to HTML",
                    why[:90])
    return False


def send_reply(
    connection_id: Optional[str],
    chat_id: int,
    text: str,
    reply_to: Optional[int],
    quote: bool = False,
    thread_id: Optional[int] = None,
    markdown: bool = False,
) -> None:
    # Telegram hard-limits messages to 4096 characters. With formatting on, the
    # cut is made on the Markdown and each piece converted separately, so a
    # chunk boundary can never land inside a tag.
    # Best first: Telegram renders the Markdown itself, tables and all.
    if markdown and send_rich(connection_id, chat_id, text, reply_to, thread_id):
        return

    if markdown:
        pieces = split_markdown(text, 3500)
    else:
        pieces = [text[i:i + 4000] for i in range(0, len(text), 4000)] or [text]

    for chunk in pieces:
        params: Dict[str, Any] = {"chat_id": chat_id, "text": chunk}
        if markdown:
            params["text"] = md_to_html(chunk)
            params["parse_mode"] = "HTML"
        if connection_id:
            params["business_connection_id"] = connection_id
        # In a forum this is not decoration: without it the answer is posted to
        # General instead of the topic it belongs to, where nobody who asked is
        # looking.
        if thread_id:
            params["message_thread_id"] = thread_id
        # Off by default in 1:1 chats - a person answering their own chat just
        # writes back. In a group, quoting is how anyone knows who you mean.
        if reply_to and (QUOTE_REPLIES or quote):
            # allow_sending_without_reply: the message may have been deleted
            # while the answer was being written, and losing the whole reply
            # over a missing quote would be worse than quoting nothing.
            params["reply_parameters"] = {"message_id": reply_to,
                                          "allow_sending_without_reply": True}
            reply_to = None  # only the first chunk quotes

        data = tg_raw("sendMessage", **params)
        if data.get("ok"):
            continue
        why = str(data.get("description") or "").lower()

        # Two things can be re-sent, because a refusal means nothing was
        # posted. Losing an answer to either would be much worse than the
        # blemish of sending it without the trimming.
        if thread_id and "thread" in why:
            # The topic was closed or deleted while we were composing.
            log.warning("  -> topic %s is gone, posting to General instead",
                        thread_id)
            params.pop("message_thread_id")
            thread_id = None
            data = tg_raw("sendMessage", **params)
            if data.get("ok"):
                continue
            why = str(data.get("description") or "").lower()

        if markdown and params.get("parse_mode") and (
                "pars" in why or "entit" in why or "tag" in why):
            # The converter produced something Telegram would not take. The
            # text is fine; only the markup is not. Send the words.
            log.warning("  -> Telegram refused the formatting (%s) - "
                        "sending it plain", why[:80])
            params.pop("parse_mode")
            params["text"] = chunk
            tg("sendMessage", **params)


def pace_and_send(
    connection_id: Optional[str],
    chat_id: int,
    incoming: str,
    answer: str,
    reply_to: Optional[int],
    cancel: threading.Event,
    spent: float,
    quote: bool = False,
    thread_id: Optional[int] = None,
) -> bool:
    """Wait like a person would, then send - unless the reply gets called off."""
    if cancel.is_set():
        return False

    pause = read_delay(incoming) - spent
    if pause > 0:
        log.info("  -> noticing the message in %.1fs", pause)
    if not wait_unless_cancelled(cancel, pause):
        return False

    with Typing(connection_id, chat_id, thread_id):
        pause = typing_delay(answer, 0.0)
        log.info("  -> typing %.1fs for %d chars", pause, len(answer))
        if not wait_unless_cancelled(cancel, pause):
            return False

    send_reply(connection_id, chat_id, answer, reply_to, quote=quote,
               thread_id=thread_id)
    log.info("  -> replied: %r", answer[:60])
    return True


# --------------------------------------------------------------------------
# Gemini
# --------------------------------------------------------------------------


def pick_working_model() -> str:
    """Confirm GEMINI_MODEL works and build the fallback list behind it."""
    global GEMINI_MODEL, MODEL_CANDIDATES
    try:
        r = session.get(
            f"{GEMINI_API}/models",
            headers=gemini_headers(),
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
    log.info("using Gemini model %s (fallbacks: %s)",
             GEMINI_MODEL, ", ".join(MODEL_CANDIDATES[1:3]) or "none")
    return GEMINI_MODEL


# Model names churn constantly, and a provider's catalogue is full of things
# that are not chatbots. These never are.
# Models that narrate their reasoning before answering. Fine for a reply, but
# ruinous for the judge: the monologue eats its whole token budget and the
# verdict never arrives.
REASONING_MARKERS = ("reasoning", "thinking", "-r1", "deepthink")

NOT_A_CHAT_MODEL = (
    "guard", "whisper", "tts", "embed", "rerank", "moderation", "safety",
    "ocr", "asr", "transcribe", "diffusion", "image", "vision", "audio",
    "speech", "reward", "classifier",
    # Groq's "compound" systems are agents with built-in web search and code
    # execution, not plain chat models. They score near zero on the name
    # heuristic, which put them at the very bottom of the ladder - and the
    # bottom is exactly where the judge starts looking.
    "compound",
)


# Rough intelligence ranking. It is a heuristic on names, because that is all
# a provider gives us, but it reliably separates a 70B instruct model from an
# 8B one - which is the difference between a good line and a bad one.
QUALITY_FAMILY = (
    ("gemini-3.8", 120), ("gemini-3.7", 112), ("gemini-3.6", 105),
    ("gemini-3.5", 98), ("gemini", 90),
    ("gpt-5", 120), ("gpt-4", 95), ("gpt-oss", 55),
    ("llama-4", 95), ("llama-3.3", 85), ("llama-3", 70), ("llama", 60),
    ("deepseek", 85), ("qwen3", 75), ("qwen", 65),
    ("mistral-large", 90), ("mistral", 60), ("magistral", 70),
    ("nemotron", 60), ("gemma", 50), ("phi", 35),
)


def model_quality(name: str) -> int:
    """How good is this model likely to be at holding a sharp conversation?"""
    n = name.lower()
    q = 0
    for family, weight in QUALITY_FAMILY:
        if family in n:
            q += weight
            break
    size = model_size(n)
    if size == 999:
        q += 35                                     # unnamed size: assume mid
    else:
        # Past ~120B the extra parameters buy little for a chat line and cost a
        # lot of queueing on a free tier, so the curve flattens and then turns.
        q += min(size, 120)
        if size > 250:
            q -= 25
    for small, penalty in (("lite", 35), ("mini", 30), ("nano", 40),
                           ("small", 25), ("tiny", 50), ("instant", 20),
                           ("flash", 8), ("scout", 15)):
        if small in n:
            q -= penalty
    # Chain-of-thought models are slow and wordy for a one-line persona.
    if any(x in n for x in REASONING_MARKERS):
        q -= 45
    if any(x in n for x in ("instruct", "chat", "-it", "versatile")):
        q += 10
    return q


def score_model(name: str, provider: str) -> int:
    """How suitable is this model id for holding a conversation?"""
    n = name.lower()
    if any(bad in n for bad in NOT_A_CHAT_MODEL):
        return -1
    if provider == "openrouter" and not n.endswith(":free"):
        return -1                       # paid slugs are not what we came for

    score = 1
    for family, weight in (("llama", 4), ("qwen", 3), ("mistral", 3),
                           ("deepseek", 3), ("gpt-oss", 4), ("gpt-4", 4),
                           ("gemma", 2), ("phi", 1), ("nemo", 2)):
        if family in n:
            score += weight
    if any(good in n for good in ("instruct", "chat", "versatile", "instant", "-it")):
        score += 2
    # Chain-of-thought models spend ten seconds and 900 characters on a one-line
    # reply, and burn the judge's token budget before reaching a verdict.
    if any(slow in n for slow in REASONING_MARKERS):
        score -= 4

    size = re.search(r"(\d+)\s*b\b", n)
    if size:                            # 7B-90B is the sweet spot for a free tier
        billions = int(size.group(1))
        score += 3 if 7 <= billions <= 90 else -1
    return score


def suggested_by_error(text: str, provider: str = "") -> Optional[str]:
    """Some providers name the replacement right in the error message."""
    m = re.search(r"use this slug instead:?\s*([\w\-./:]+)", text, re.I)
    if not m:
        return None
    slug = m.group(1).rstrip(".,")
    # OpenRouter happily points at the paid twin of a retired free model.
    # Take the hint, but keep it on the free tier.
    if provider == "openrouter" and not slug.endswith(":free"):
        slug += ":free"
    return slug


def discover_models(p: Provider, limit: int = 8) -> List[str]:
    """Best chat models this provider advertises, best first.

    A catalogue entry is not a promise: free keys are routinely refused models
    that are listed, so the caller should be ready to try more than one.
    """
    headers = {"Authorization": f"Bearer {p.key}"}
    try:
        r = session.get(f"{p.base}/models", headers=headers, timeout=20)
        ids = [m.get("id") for m in (r.json().get("data") or []) if m.get("id")]
    except Exception as exc:
        log.warning("  -> could not list %s models: %s", p.name, exc)
        return []
    ranked = sorted(((score_model(i, p.name), i) for i in ids), reverse=True)
    return [i for score, i in ranked if score > 0][:limit]


def model_size(name: str) -> int:
    """Parameter count in billions, or a large number when it isn't stated."""
    m = re.search(r"(\d+)\s*b\b", name.lower())
    return int(m.group(1)) if m else 999


def replacement_models(p: Provider, error_text: str) -> List[str]:
    """What to try instead of p.model, best first."""
    out = []
    hint = suggested_by_error(error_text, p.name)
    # A provider pointing at the model that just failed is no help at all.
    if hint and hint != p.model:
        out.append(hint)
    out += [m for m in discover_models(p) if m != p.model and m not in out]
    return out


def looks_like_model_error(status: int, text: str) -> bool:
    if status not in (400, 402, 404, 410, 422, 429):
        return False
    if status == 402:
        return True                     # this model is paid; a smaller one may not be
    if status == 429 and "upstream" in text.lower():
        return True                     # this particular model is throttled, others aren't
    t = text.lower()
    return any(w in t for w in ("model", "not found", "decommission", "slug",
                                "end of life", "gone", "retired"))


THINK_BLOCK = re.compile(
    r"<\s*(think|thinking|reason|reasoning|scratchpad)\s*>.*?<\s*/\s*\1\s*>",
    re.S | re.I,
)
THINK_OPEN = re.compile(r"<\s*(think|thinking|reason|reasoning|scratchpad)\s*>", re.I)
THINK_CLOSE = re.compile(r"<\s*/\s*(think|thinking|reason|reasoning|scratchpad)\s*>", re.I)


def strip_thinking(text: str) -> str:
    """Remove a reasoning model's inner monologue from what we are about to send.

    Some models emit <think>...</think> inside the ordinary content field. That
    is not an answer, it is homework, and it must never reach a customer.
    """
    cleaned = THINK_BLOCK.sub("", text)
    # Truncated or malformed blocks: keep only what follows the last closing tag,
    # and if a block was opened and never closed, the whole thing was thinking.
    last_close = None
    for m in THINK_CLOSE.finditer(cleaned):
        last_close = m
    if last_close:
        cleaned = cleaned[last_close.end():]
    if THINK_OPEN.search(cleaned):
        cleaned = THINK_OPEN.split(cleaned)[0]
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)       # tidy the seam
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


# Typographic characters a model reaches for and a person on a phone does not.
# Not "no Unicode" - the text is Russian, it is Unicode throughout. Just the
# punctuation that no phone keyboard puts within easy reach, which is one of
# the surest tells that a machine wrote the line.
TYPOGRAPHY = {
    "\u2012": "-", "\u2013": "-", "\u2014": "-", "\u2015": "-",  # dashes
    "\u2212": "-", "\u2010": "-", "\u2011": "-",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"',  # curly "
    "\u00ab": '"', "\u00bb": '"', "\u2033": '"',
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u2032": "'",  # curly '
    "\u2026": "...",                                             # ellipsis
    "\u2022": "-", "\u2023": "-", "\u25aa": "-", "\u00b7": "-",  # bullets
    "\u00a0": " ", "\u2007": " ", "\u2009": " ", "\u202f": " ",  # odd spaces
    "\u200a": " ", "\u2005": " ", "\u3000": " ",
    "\u200b": "", "\u200c": "", "\u200d": "", "\ufeff": "",     # invisible
    "\u2116": "N", "\u2122": "", "\u00ae": "", "\u00a9": "",
    "\u2190": "<-", "\u2192": "->",
}
TYPOGRAPHY_MAP = str.maketrans(TYPOGRAPHY)


def plain_punctuation(text: str) -> str:
    """Typographic punctuation down to what a phone keyboard actually types."""
    return text.translate(TYPOGRAPHY_MAP)


def trim_reply(text: str, limit: Optional[int] = None) -> str:
    """Last-resort length guard, cutting at a sentence end where possible.

    `limit` of 0 means no guard at all, which is the default everywhere now:
    length is the prompt's business. The only caller that still passes a real
    number is the inline reply, which is edited into an existing message and so
    cannot spill into a second one.
    """
    text = plain_punctuation(text)
    limit = REPLY_MAX_CHARS if limit is None else limit
    if limit <= 0 or len(text) <= limit:
        return text
    head = text[:limit]
    cut = max(head.rfind(". "), head.rfind("! "), head.rfind("? "),
              head.rfind(".\n"), head.rfind("... "))
    return (head[:cut + 1] if cut > limit // 3 else head).strip()


class Candidate:
    """One (provider, model) rung of the ladder."""

    __slots__ = ("provider", "model", "quality", "dead", "cool_until",
                 "bad_judge", "seconds")

    def __init__(self, provider: Provider, model: str) -> None:
        self.provider = provider
        self.model = model
        self.quality = model_quality(model)
        self.dead = False           # gone for good: 404 / 402 / 410
        self.cool_until = 0.0       # rate limited: come back later
        self.bad_judge = False      # cannot produce a usable verdict
        self.seconds = 0.0          # rolling average response time, 0 = untried

    def timed(self, elapsed: float) -> None:
        self.seconds = elapsed if not self.seconds else self.seconds * 0.6 + elapsed * 0.4

    def rank(self, fast: bool) -> int:
        """Quality, discounted by how long this rung actually takes.

        In a group a brilliant answer 40 seconds late is worse than a good one
        now - the conversation has moved on. In a 1:1 chat the bot is pretending
        to type anyway, so patience is free and quality wins.
        """
        t = self.seconds
        if not t:
            return self.quality
        if fast:
            penalty = 0 if t < 4 else 40 if t < 8 else 110 if t < 15 else 220
        else:
            # In a 1:1 chat the bot is faking a reading-and-typing pause of up
            # to TYPING_MAX anyway, so a slow model costs the reader nothing.
            # The discount here is only a tiebreak between rungs of similar
            # quality; it must never outrank a genuinely cleverer model.
            penalty = 0 if t < 10 else 15 if t < 25 else 45
        return self.quality - penalty

    @property
    def usable(self) -> bool:
        return (not self.dead and time.time() >= self.cool_until
                and not self.provider.parked)

    def __repr__(self) -> str:
        return f"{self.provider.name}/{self.model}({self.quality})"


LADDER: List[Candidate] = []


def ladder_order(fast: bool = False) -> List[Candidate]:
    """Every rung, cleverest first. The one ranking the whole bot agrees on.

    Quality is discounted by measured latency, so `fast` - a room, where a
    brilliant answer forty seconds late lands after the conversation moved on -
    reshuffles the top without changing the principle: always start with the
    best model still standing and walk down only as the good ones run out.
    """
    return sorted(LADDER, key=lambda c: -c.rank(fast))


def build_ladder() -> None:
    """Ask every provider what it serves, then rank the lot by quality.

    The point is to answer with the best model available anywhere, not with
    whatever the first provider happens to offer. Everything that picks a model
    afterwards - replies in every mode, and the group judge - walks this same
    list through ladder_order(), cleverest first, stepping down only as the
    good rungs run out of quota.
    """
    global LADDER
    rungs: List[Candidate] = []
    for p in PROVIDERS:
        if p.name == "gemini":
            models = list(MODEL_CANDIDATES or [GEMINI_MODEL])
        else:
            models = [p.model] if p.model else []
            models += [m for m in discover_models(p, limit=6) if m not in models]
        for m in models:
            rungs.append(Candidate(p, m))
    LADDER = sorted(rungs, key=lambda c: -c.quality)
    log.info("model ladder (%d rungs): %s", len(LADDER),
             ", ".join(f"{c.provider.name}/{c.model}" for c in LADDER[:6]))


def openai_turns(key: str, user_text: Optional[str]) -> List[dict]:
    msgs = [
        {"role": "user" if h["role"] == "user" else "assistant", "content": h["text"]}
        for h in history[key]
    ]
    if user_text is not None:
        msgs.append({"role": "user", "content": user_text})
    return msgs


def openai_call(
    p: Provider,
    model: str,
    system: str,
    messages: List[dict],
    max_tokens: int,
    json_mode: bool = False,
    timeout: Optional[int] = None,
    max_chars: Optional[int] = None,
) -> tuple:
    """One call to an OpenAI-compatible endpoint. Returns (text|None, status)."""
    body: Dict[str, Any] = {
        "model": model,
        "messages": ([{"role": "system", "content": system}] if system else []) + messages,
        "temperature": 0.2 if json_mode else TEMPERATURE,
        "max_tokens": max_tokens,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    if p.name == "groq" and not p.no_reasoning_param:
        # Groq can drop a reasoning model's monologue server-side, which is both
        # cheaper and safer than us cleaning it up afterwards.
        body["reasoning_format"] = "hidden"
    headers = provider_headers(p)

    started = time.time()
    try:
        r = session.post(f"{p.base}/chat/completions", headers=headers, json=body,
                         timeout=timeout or GEMINI_TIMEOUT)
    except Exception as exc:
        log.warning("  -> %s/%s failed after %.0fs: %s",
                    p.name, model, time.time() - started, exc)
        return None, 0

    if r.status_code != 200:
        if (r.status_code == 400 and "reasoning_format" in r.text
                and not p.no_reasoning_param):
            p.no_reasoning_param = True
            log.info("  -> %s rejects reasoning_format, retrying without it", p.name)
            return openai_call(p, model, system, messages, max_tokens, json_mode, timeout)
        if json_mode and r.status_code == 400 and "json" in r.text.lower():
            if not p.no_json:
                log.warning("  -> %s cannot do strict JSON mode, using plain text", p.name)
            p.no_json = True
            return None, r.status_code
        log.warning("  -> %s/%s %s: %s", p.name, model, r.status_code,
                    r.text[:120].replace("\n", " "))
        return None, r.status_code

    try:
        text = r.json()["choices"][0]["message"]["content"]
    except Exception as exc:
        log.warning("  -> %s gave unusable output: %s", p.name, exc)
        return None, 0

    raw_len = len(text or "")
    text = strip_thinking(text or "")
    if raw_len and not text:
        log.warning("  -> %s/%s sent %d chars of pure reasoning and no answer",
                    p.name, model, raw_len)
        return None, 0
    if len(text) < raw_len:
        log.info("  -> stripped %d chars of <think>", raw_len - len(text))
    if not text:
        log.warning("  -> %s returned an empty message", p.name)
        return None, 0
    if not json_mode:
        text = trim_reply(text, max_chars)
    log.info("  -> %s/%s 200 in %.1fs, %d chars",
             p.name, model, time.time() - started, len(text))
    return text, 200


def gemini_call(model: str, system: str, contents: List[dict],
                max_tokens: int, thinking: bool = True,
                max_chars: Optional[int] = None) -> tuple:
    """One Gemini generateContent call. Returns (text|None, status)."""
    gen: Dict[str, Any] = {"temperature": TEMPERATURE, "maxOutputTokens": max_tokens}
    if thinking and THINKING_LEVEL:
        gen["thinkingConfig"] = {"thinkingLevel": THINKING_LEVEL}
    started = time.time()
    try:
        r = session.post(
            gemini_url(model), headers=gemini_headers(),
            json=({"system_instruction": {"parts": [{"text": system}]}} if system else {}) | {
                "contents": contents, "generationConfig": gen},
            timeout=GEMINI_TIMEOUT,
        )
    except Exception as exc:
        log.warning("  -> gemini/%s failed: %s", model, exc)
        return None, 0

    if r.status_code == 400 and thinking and "think" in r.text.lower():
        return gemini_call(model, system, contents, max_tokens, False, max_chars)
    if r.status_code != 200:
        log.warning("  -> gemini/%s %s: %s", model, r.status_code,
                    r.text[:120].replace("\n", " "))
        return None, r.status_code

    data = r.json()
    cands = data.get("candidates") or []
    if not cands:
        log.warning("  -> gemini: no candidates (%s)", data.get("promptFeedback"))
        return None, 0
    cand = cands[0]
    text = gemini_text(cand)
    if not text:
        reason = cand.get("finishReason")
        log.warning("  -> gemini/%s produced no text (finishReason=%s)", model, reason)
        if reason == "MAX_TOKENS" and max_tokens < 4096:
            return gemini_call(model, system, contents, 4096, False, max_chars)
        return None, 0
    log.info("  -> gemini/%s 200 in %.1fs, %d chars",
             model, time.time() - started, len(text))
    return trim_reply(text, max_chars), 200


def note_failure(c: Candidate, status: int) -> None:
    """Retire or rest a rung, so the next message does not repeat the mistake."""
    if status in (400, 402, 404, 410, 422):
        c.dead = True
        log.info("  -> retiring %s", c)
    elif status == 429:
        c.cool_until = time.time() + MODEL_COOLDOWN
        log.info("  -> %s rate limited, resting it %d min", c, MODEL_COOLDOWN // 60)
    else:
        c.cool_until = time.time() + 60


def ask_ai(
    key: str,
    user_text: Optional[str],
    extra_system: str = "",
    cancel: Optional[threading.Event] = None,
    fast: bool = False,
    room: bool = False,
    max_chars: Optional[int] = None,
    raw: bool = False,
    who: Optional[dict] = None,
    private: bool = False,
    where: str = "",
) -> Optional[str]:
    """Answer with the best model still available, walking down the ladder.

    `room` is a group or an inline summon, where the prompt tells him to write
    as long as the subject deserves; a 1:1 chat keeps the one-line persona.
    Length is the prompt's business: by default nothing is truncated, because
    cutting at a character count only ever chopped somebody off mid-sentence.
    The one caller that passes a real `max_chars` is the inline reply, which is
    edited into an existing message and so cannot spill into a second one.
    """
    if not LADDER:
        log.error("no models available at all")
        return None

    if max_chars is None:
        max_chars = ROOM_MAX_CHARS if room else REPLY_MAX_CHARS

    # `raw` drops the character entirely: no persona, no house style, no date,
    # no history. What is left is `extra_system` on its own - empty for a bare
    # question, or a single instruction like "one short sentence". Everything
    # mechanical still applies; the length cap and the punctuation cleanup are
    # about what Telegram and a phone keyboard can do, not about how to behave.
    # The same facts reach every mode. What differs is how they are wrapped:
    # raw has nothing else in its system prompt, the character has everything
    # else. `private` decides whether the unpublished half - your pin, your
    # /ctx note - is included; see context_block.
    facts = context_block(who, private=private, where=where, plain=raw)

    # Everything this chat said before the window starts. Same block in every
    # mode, and it is the only reason a conversation past HISTORY_TURNS still
    # knows the name agreed on its first day.
    recap = summaries.get(key, "")
    if recap:
        recap = ("Earlier in this conversation, before the messages below - "
                 "your own notes, not something they said just now:\n" + recap)

    if raw:
        system = "\n\n".join(p for p in (facts, recap, extra_system.strip()) if p)
    else:
        system = (PERSONA + SYSTEM_SUFFIX + extra_system
                  + (f"\n{facts}\n" if facts else now_line())
                  + (f"\n{recap}\n" if recap else ""))
    gem_contents = [{"role": h["role"], "parts": [{"text": h["text"]}]}
                    for h in history[key]]
    if user_text is not None:
        gem_contents.append({"role": "user", "parts": [{"text": user_text}]})
    oai_messages = openai_turns(key, user_text)

    order = ladder_order(fast)
    best = order[0]
    for c in order:
        if cancel is not None and cancel.is_set():
            return None
        if not c.usable:
            continue

        started = time.time()
        if c.provider.name == "gemini":
            text, status = gemini_call(c.model, system, gem_contents,
                                       MAX_OUTPUT_TOKENS, max_chars=max_chars)
        else:
            text, status = openai_call(c.provider, c.model, system, oai_messages,
                                       MAX_OUTPUT_TOKENS, max_chars=max_chars)
        if text:
            c.timed(time.time() - started)
            if fast and c.seconds > SLOW_SECONDS:
                log.info("  -> %s took %.0fs; it drops down the ladder for groups",
                         c, c.seconds)
            c.provider.revive()
            c.provider.model = c.model          # for /status and /check
            if c is not best:
                log.warning("  -> answered by %s (better rungs unavailable)", c)
            return text
        note_failure(c, status)
        if status in (401, 403):
            c.provider.park()          # the key, not the model, is the problem

    log.error("  -> every rung of the ladder failed")
    return None


# --------------------------------------------------------------------------
# Update handling
# --------------------------------------------------------------------------


def handle_business_message(msg: dict, cancel: Optional[threading.Event] = None) -> None:
    cancel = cancel if cancel is not None else NEVER_CANCELLED
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

    # Only serve the account this bot belongs to. Fails closed: if we cannot
    # establish whose connection this is, we say nothing.
    if OWNER_ID:
        if owner_id is None:
            return skip("cannot confirm whose business account this is")
        if owner_id != OWNER_ID:
            return skip("connection belongs to %s, not you - ignoring", owner_id)

    # A 1:1 chat on your own account is proof you know this person - better
    # proof than sharing a group with them, in fact. Recorded before the
    # "sent by you" branch below, so writing to a friend counts as much as
    # them writing to you. In a private chat the other person's user id IS
    # the chat id, which is why this needs no lookup of any kind.
    if INLINE_TRUST_DM and INLINE_ACCESS == "shared":
        if allow_inline_for(chat_id, chat_id):
            log.info("inline: %s writes with you in private - allowed for %d days",
                     chat_id, INLINE_MEMBER_DAYS)

    if owner_id is not None and sender.get("id") == owner_id:
        # This is you writing to the customer - record it as context, don't reply.
        if text:
            remember(f"{connection_id}:{chat_id}", "model", text)
        return skip("sent by you (the business owner), kept as context")
    if owner_id is None and sender.get("id") != chat_id:
        # Fallback heuristic: in a private chat the customer's own id equals the
        # chat id, so anything else is the owner's outgoing message.
        if text:
            remember(f"{connection_id}:{chat_id}", "model", text)
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
    if REPLY_COOLDOWN and time.time() - last_reply_at.get(key, 0) < REPLY_COOLDOWN:
        return skip("within REPLY_COOLDOWN of the last reply")

    user_text = text[:MAX_INPUT_CHARS]
    log.info("  -> answering...")

    # Generate first, while the chat still looks untouched - as far as the other
    # side is concerned the phone is still face down on a table somewhere.
    started = time.time()
    answer = ask_ai(key, user_text, cancel=cancel, who=sender,
                    where="a 1:1 chat on your business account")
    if not answer:
        log.warning("  -> Gemini returned nothing, no reply sent")
        return

    # thread_of is None in a chat without threads, which is every business
    # chat today - but the rule is the same everywhere, so if Telegram ever
    # threads these too, the reply is already going back where it came from.
    if not pace_and_send(connection_id, chat_id, user_text, answer,
                         msg.get("message_id"), cancel, time.time() - started,
                         thread_id=thread_of(msg)):
        return

    remember(key, "user", user_text)
    remember(key, "model", answer)
    compact(key)
    last_reply_at[key] = time.time()

    with _pending_guard:
        p = pending.get(key)
        if p and p.cancel is cancel:
            pending.pop(key, None)


# The room rules, shared by every place where he is one voice among several:
# a group he was added to, and an inline summon into a chat he cannot see. The
# base persona is written for a 1:1 chat - one line, and take apart the remark
# rather than the person, because the other end may be a customer. Neither of
# those is right in a room, so both are overridden here.
ROOM_RULES = """
LENGTH - THERE IS NO LIMIT HERE
Write as much as you want. One word, one line, or five paragraphs of a rant \
that has been building for years: whatever the subject actually deserves. This \
is a conversation among people, not a support desk, and nobody is counting \
your sentences. Length should follow the thought and stop when the thought \
stops - not because you have run out of allowance, and not padded to look \
substantial when a single line would land harder.

Write it as a person types in a chat: plain prose, paragraphs if the thing \
needs them. No lists, no bullet points, no headings, no bold, no emoji - \
nobody formats a group message like a report.

TRADING INSULTS - THIS OVERRIDES THE ONE-TO-ONE RULE ABOVE
These are people who know each other, not a customer chat. If someone comes at \
you, you give as good as you get: mock them back, call them what they are, and \
swear if they swore first. Aim it at the person who started it and make it \
funny - the point is that they picked the wrong opponent, not that you are \
angry. Wit beats volume, so a comeback usually lands hardest short, but that \
is a matter of aim, not a word limit: if the thing deserves a paragraph, write \
the paragraph. A specific jab about what they just said beats any generic \
insult, at any length.

Where you stop, and this is absolute: nothing about anyone's ethnicity, \
nationality, religion, gender, sexuality, disability or illness; no threats \
of any kind; nothing sexual about them; nothing about their family; and no \
piling on a person somebody else is already dogpiling. Those are not \
comebacks, they are just ugly, and they would land on your account.

You never start it. You answer a jab once and let it go, rather than circling \
back to it three messages later. If they stop, you stop - and if they are \
plainly upset rather than playing, you drop the whole thing at once.
"""

GROUP_NOTE = """

GROUP CHAT - THIS OVERRIDES THE ONE-LINE RULE
You are in a group with several people. Every incoming line is prefixed with \
the name of whoever said it. Never prefix your own replies with a name; use \
someone's name only when it matters who you are answering.

You are only ever writing here because somebody addressed you or was talking \
about you. Answer that - the person and the thing they actually said. Never \
open with an offer to help and never ask whether they need assistance.
""" + ROOM_RULES


INLINE_NOTE = """

SUMMONED INTO A CHAT YOU CANNOT SEE - THIS OVERRIDES THE ONE-LINE RULE
Somebody typed your name in a chat and handed you one line. That line is \
everything you get: no history, no names, no idea who else is in the room, and \
nothing is prefixed with who said it. Do not ask to be filled in, do not guess \
who is speaking, and never refer to "this chat" or to what anyone supposedly \
said before - you were not there.

Answer the line in front of you as if it had been said to your face. No \
greeting, no sign-off, and never an offer to help.
""" + ROOM_RULES


def display_name(user: dict) -> str:
    name = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x)
    return name or user.get("username") or f"user{user.get('id')}"


NAME_PATTERN = re.compile(
    r"\b(?:" + "|".join(re.escape(n) for n in GROUP_NAMES) + r")\b", re.I | re.U
) if GROUP_NAMES else None


def called_by_name(text: str) -> Optional[str]:
    """Did somebody use one of his names? Exact words only, no stemming."""
    if not NAME_PATTERN:
        return None
    m = NAME_PATTERN.search(text)
    return m.group(0) if m else None


def group_allowed(chat_id: int) -> bool:
    """Serve a group only if it is one of yours.

    An explicit allowlist wins. Otherwise, if OWNER_ID is set, the test is
    simply whether you are in that group - a stranger who adds the bot to
    their own chat gets nothing.
    """
    if GROUP_ALLOWLIST:
        return chat_id in GROUP_ALLOWLIST
    if not OWNER_ID:
        return True

    cached = group_ok_cache.get(chat_id)
    if cached and time.time() - cached[1] < 3600:
        return cached[0]

    res = tg("getChatMember", chat_id=chat_id, user_id=OWNER_ID)
    ok = bool(res) and res.get("status") not in ("left", "kicked")
    group_ok_cache[chat_id] = (ok, time.time())
    if not ok:
        log.warning("group %s: you are not in it - ignoring", chat_id)
        if GROUP_AUTO_LEAVE:
            log.warning("leaving group %s", chat_id)
            tg("leaveChat", chat_id=chat_id)
    return ok


JUDGE_PROMPT = """\
You decide one thing: whether Ilya should say something in this group chat \
right now. Nothing else.

Ilya is a member of this group. Forty-five, lives in Dresden, dry and \
sardonic, strong opinions about technology, AI, bots, and how much better \
things supposedly used to be. He is not a helper and not an assistant - he is \
a guy in a chat who talks when he actually has something to say.

You are given the last lines of the conversation, oldest first, each labelled \
with who said it. Ilya's own lines are labelled "Ilya".

Somebody may have used one of his names in the last line - Ilya, Ilyusha, or \
simply "bot", which is what people in this group call him. If they are \
ADDRESSING him with it, he answers: that is being spoken to, and ignoring it \
is rude. But the same words also come up when people are merely talking ABOUT \
bots, or about some other Ilya, and then it is not his cue. Read which one it \
is; the note under the transcript tells you a name was used, not that he was \
addressed.

THE ONLY REASON HE SPEAKS IS THAT THE ROOM INVOLVED HIM.
- Somebody addresses him: by name, or by aiming a question at him plainly \
enough that everyone would read it as his to answer.
- Somebody talks about him: asks where he went, wonders why he is quiet, \
refers to him in the third person, quotes him, argues with something he said, \
complains about him, or answers a point of his.
- Somebody replies to a line of his and clearly expects something back.
- Somebody needles him: an insult, a jab, mockery, calling him a soulless \
bot, telling him to shut up, daring him to say something. This counts even \
when it is a single word, even with no name attached, and even if he has just \
spoken. Being needled and going quiet reads as having lost, and he does not \
lose.

Those four always get an answer.

HE MAY ALSO JOIN A CONVERSATION THAT IS NOT ABOUT HIM - but only when he has \
something genuinely worth adding, and only with the restraint of a man who \
knows the difference between contributing and interrupting. Reasons good \
enough: the subject is one he actually knows something about; somebody said \
something sweeping or plainly wrong; a question is hanging in the air that \
nobody has answered; the room is joking and a good line would land.

Reasons that are NOT good enough: the topic is merely interesting; he has an \
opinion; he could be funny about it. Everyone could. That is not a reason.

Weigh how recently he spoke - you are told how many seconds ago. This applies \
only to joining a topic; being addressed, discussed or needled overrides it \
entirely. If he has just said something and nothing new has been put to him, \
he stays quiet: two \
uninvited lines in a row from the same person is where a chat member becomes \
a nuisance. The longer he has been silent, the more freely he may join in.

Stay quiet as well when:
- one of his names appears but the talk is about bots or software in general, \
or about a different Ilya, rather than to him or about him;
- the mention of him is finished business - somebody thanked him, agreed, or \
signed off, and nothing is being put to him;
- two people are settling something private, practical or logistical;
- it is greetings, one-word noise, stickers, links without comment;
- the subject is serious, sad, medical, financial or otherwise sensitive;
- he already made this point in the recent lines - repeating it is worse than \
silence.

When genuinely unsure, stay quiet. Silence costs nothing; a man who inserts \
himself into conversations that were not about him is exhausting.
"""

JUDGE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "speak": {"type": "BOOLEAN"},
        "reason": {"type": "STRING"},
    },
    "required": ["speak", "reason"],
}


def judge_order() -> List[Candidate]:
    """The rungs the judge may use - the DUMBEST first, climbing up.

    Deliberately the opposite direction from replies. A verdict is a yes/no, so
    the cheap rungs are good enough for it, and every judge call spent on a
    clever model is one the actual answers no longer have. It climbs only when
    a rung turns out unable to produce a usable verdict at all.

    GROUP_JUDGE_MODEL, when set to a model that is actually on the ladder, is
    tried before everything else.
    """
    usable = [c for c in LADDER if c.usable and not c.bad_judge]
    # The cheap end of the ladder collects models for two different reasons:
    # being small, and being unsuitable. Anything under the quality floor, or
    # that narrates its reasoning, is skipped here - those score low because
    # the heuristic cannot place them, not because they are small-but-fine.
    # They stay on the ladder for replies. If nothing clears the bar, a bad
    # judge still beats no judge.
    fit = [c for c in usable
           if c.quality >= GROUP_JUDGE_MIN_QUALITY
           and not any(m in c.model.lower() for m in REASONING_MARKERS)]
    order = sorted(fit or usable, key=lambda c: c.quality)
    if GROUP_JUDGE_MODEL:
        pinned = [c for c in order if c.model == GROUP_JUDGE_MODEL]
        if pinned:
            order = pinned + [c for c in order if c not in pinned]
    return order


SUMMARY_PROMPT = (
    "You keep notes on a conversation so that nothing important is lost when "
    "old messages scroll out of the window.\n"
    "You are given the notes so far and the messages that are about to be "
    "dropped. Rewrite the notes so they cover both.\n"
    "\n"
    "KEEP: names, numbers, prices, dates, addresses, decisions taken, things "
    "promised, preferences and dislikes stated, open questions, anything the "
    "person asked to be remembered, and how the relationship stands.\n"
    "DROP: greetings, small talk, pleasantries, anything already implied by "
    "what is kept, and anything that was only true at the time.\n"
    "\n"
    "Write it as compact prose in the language of the conversation - no "
    "bullets, no headings, no preamble. If the old notes and the new messages "
    "disagree, the new messages win. Output the notes and nothing else."
)


def summarise(old: str, turns: List[Dict[str, str]]) -> Optional[str]:
    """Fold a batch of turns into the running notes.

    Runs on the CHEAP end of the ladder, like the judge: condensing is
    mechanical work, and a summarisation call made on the clever model is one
    the actual answers no longer have.
    """
    lines = [("them: " if t["role"] == "user" else "you: ") + t["text"]
             for t in turns]
    question = (
        (f"NOTES SO FAR:\n{old}\n\n" if old else "")
        + "MESSAGES ABOUT TO BE DROPPED:\n" + "\n".join(lines)
        + f"\n\n---\nRewrite the notes. At most {SUMMARY_MAX_CHARS} characters."
    )

    for c in judge_order():
        if c.provider.name == "gemini":
            text, _ = gemini_call(
                c.model, SUMMARY_PROMPT,
                [{"role": "user", "parts": [{"text": question}]}],
                max_tokens=1200, max_chars=SUMMARY_MAX_CHARS)
        else:
            text, _ = openai_call(
                c.provider, c.model, SUMMARY_PROMPT,
                [{"role": "user", "content": question}],
                max_tokens=1200, timeout=25, max_chars=SUMMARY_MAX_CHARS)
        if text and text.strip():
            log.info("  notes rewritten by %s/%s: %d chars",
                     c.provider.name, c.model, len(text))
            return text.strip()[:SUMMARY_MAX_CHARS]
    log.warning("  could not rewrite the notes - the turns stay in the window "
                "and it will be tried again")
    return None


def compact(key: str) -> None:
    """Make room in the window by turning its oldest turns into notes.

    Called after a reply has already gone out, so the wait costs the person
    nothing: they have their answer, and the tidying happens before the next
    message in this chat is handled.
    """
    if not SUMMARY_ENABLED or not key or key == INLINE_KEY:
        return
    extra = len(history[key]) - HISTORY_TURNS
    if extra <= 0:
        return

    turns = list(history[key])[:extra]
    fresh = summarise(summaries.get(key, ""), turns)
    if not fresh:
        return                      # leave them in place and try again later

    for _ in range(min(extra, len(history[key]))):
        history[key].popleft()
    summaries[key] = fresh
    history_seen[key] = time.time()
    with _history_lock:
        dirty_keys.add(key)
    history_dirty.set()
    log.info("  %s: %d oldest turns folded into notes, %d kept verbatim",
             key, extra, len(history[key]))


def judge_model() -> str:
    """Whichever rung the judge would reach for right now."""
    order = judge_order()
    return repr(order[0]) if order else "none"


def judge_budget_ok(key: str) -> bool:
    """The budget is per CHAT, not per topic.

    Transcripts are split by forum topic, but the quota it protects is not:
    a forum with ten busy topics would otherwise be ten times the judge calls
    per minute, on a free tier that has one limit for all of them.
    """
    return rate_ok(judge_calls, key.split(":", 2)[1] if ":" in key else key,
                   GROUP_JUDGE_MAX_PER_MIN)


def should_speak(key: str, quiet_for: float, name_used: Optional[str] = None) -> Optional[str]:
    """Ask a cheap model whether Ilya would naturally jump in right now.

    Returns the reason to speak, "" when the judge says stay quiet, and None
    when the call itself failed (so the caller can fall back to keywords).
    """
    lines = [h["text"] if h["role"] == "user" else f"Ilya: {h['text']}"
             for h in list(history[key])[-GROUP_JUDGE_TURNS:]]
    transcript = "\n".join(lines)
    note = ""
    if name_used:
        note = (f'The last line contains the word "{name_used}". Decide whether '
                f"it is addressed to Ilya or merely mentions it.\n")
    question = (
        f"{transcript}\n\n---\n"
        f"{note}"
        f"Ilya last said something here {int(quiet_for)} seconds ago.\n"
        f"Should he say something now?"
    )

    # Same ladder as replies, walked the other way: cheapest rung first, and it
    # only climbs when one turns out no good at producing a verdict.
    for c in judge_order():
        verdict, outcome = judge_via(c, question)
        if verdict is not None:
            reason = str(verdict.get("reason", ""))[:80]
            if verdict.get("speak"):
                return f"context: {reason}"
            log.info("  staying quiet - %s", reason)
            return ""
        if outcome == "unusable":
            # It answered and still could not produce a verdict. It will do the
            # same next time, so stop asking it - for good.
            c.bad_judge = True
            log.info("  judge: %s cannot produce verdicts, dropping it", c)
        else:
            # Quota, network, a 5xx. Nothing to do with its judgement; skip it
            # this once and let the cooldown decide when it comes back.
            log.info("  judge: %s unavailable right now, trying the next rung", c)
    return None


def parse_json_loose(raw: str) -> Optional[dict]:
    """Models wrap JSON in prose, fences and apologies. Dig it out anyway."""
    try:
        return json.loads(raw)
    except Exception:
        pass
    m = re.search(r"\{.*\}", raw, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    # Last resort: the only thing that matters is yes or no.
    low = raw.lower()
    if '"speak"' in low or "speak" in low:
        if re.search(r"speak\W{0,4}(true|yes)", low):
            return {"speak": True, "reason": "parsed from loose output"}
        if re.search(r"speak\W{0,4}(false|no)", low):
            return {"speak": False, "reason": "parsed from loose output"}
    return None


JUDGE_JSON_HINT = ('\n\nAnswer with JSON and nothing else - no prose, no code '
                   'fences: {"speak": true, "reason": "a few words"}')


def judge_via(c: Candidate, question: str) -> tuple:
    """Run the speak/stay-quiet decision on one rung.

    Returns (verdict, outcome). The outcome matters as much as the verdict:

      "ok"          - got a usable verdict
      "unusable"    - the model answered, but cannot produce a verdict at all
                      (prose instead of JSON, empty output). Worth remembering:
                      it will do the same thing next time.
      "unavailable" - the call itself did not land: network, 429, 5xx. Says
                      nothing about the model's ability to judge, so it must
                      NOT be held against it - the rung is simply skipped now
                      and tried again on the next message.
    """
    p = c.provider
    if p.name != "gemini":
        # Strict JSON mode is the good path, but several free models cannot
        # honour it and answer 400. Falling back to plain text plus a tolerant
        # parser is far better than losing the judge entirely.
        modes = (False,) if p.no_json else (True, False)
        answered = False
        for strict in modes:
            raw, status = openai_call(
                p, c.model, JUDGE_PROMPT + JUDGE_JSON_HINT,
                [{"role": "user", "content": question}],
                max_tokens=600, json_mode=strict, timeout=20,
            )
            if not raw:
                if status:
                    note_failure(c, status)      # a 429 here rests it properly
                continue
            answered = True
            verdict = parse_json_loose(raw)
            if verdict is not None:
                return verdict, "ok"
            log.warning("  judge: %s gave unparseable output", p.name)
        return None, ("unusable" if answered else "unavailable")

    model = c.model
    url, headers = gemini_url(model), gemini_headers()

    def body(thinking: bool) -> dict:
        gen: Dict[str, Any] = {
            "temperature": 0.2,
            "maxOutputTokens": 1024,
            "responseMimeType": "application/json",
            "responseSchema": JUDGE_SCHEMA,
        }
        if thinking:
            gen["thinkingConfig"] = {"thinkingLevel": "low"}
        return {
            "system_instruction": {"parts": [{"text": JUDGE_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": question}]}],
            "generationConfig": gen,
        }

    for thinking in (True, False):
        try:
            r = session.post(url, headers=headers, json=body(thinking), timeout=20)
        except Exception as exc:
            log.warning("  judge call failed: %s", exc)
            return None, "unavailable"
        if r.status_code == 400 and thinking and "think" in r.text.lower():
            continue
        if r.status_code != 200:
            log.warning("  judge %s: %s", r.status_code, r.text[:120].replace("\n", " "))
            note_failure(c, r.status_code)
            return None, "unavailable"
        try:
            return json.loads(gemini_text(r.json()["candidates"][0])), "ok"
        except Exception as exc:
            log.warning("  judge gave unusable output: %s", exc)
            return None, "unusable"
    return None, "unavailable"


def group_trigger(msg: dict, text: str, key: str) -> Optional[str]:
    """Why we should speak up in this group, or None to stay quiet."""
    if GROUP_REPLY_ALL or GROUP_TRIGGER == "all":
        return "reply-all is on"

    # Being spoken to directly always wins - no judging, no cooldown.
    replied = msg.get("reply_to_message") or {}
    if (replied.get("from") or {}).get("id") == BOT_ID:
        return "replying to us"
    if BOT_USERNAME and f"@{BOT_USERNAME}".lower() in text.lower():
        return "mentioned"

    # A name skips every guard below - but the judge still decides, because
    # "бот, расскажи анекдот" and "нам нужен бот для склада" look identical to
    # a regex and nothing alike to a reader.
    name = called_by_name(text)

    since = time.time() - last_interjection.get(key, 0)
    if not name:
        if GROUP_COOLDOWN and since < GROUP_COOLDOWN:
            log.info("%s | said something %.0fs ago, staying out of it "
                     "(GROUP_COOLDOWN=%.0fs)", key, since, GROUP_COOLDOWN)
            return None

        recent = list(history[key])[-GROUP_JUDGE_RECENT_TURNS:]
        in_the_exchange = any(h["role"] == "model" for h in recent)

        # A three-word jab right after he spoke is almost certainly aimed at
        # him - "дурак", "ну и бот", "заткнись". Those are exactly the messages
        # the length floor would throw away, so it only applies to a room he is
        # not part of.
        if len(text) < GROUP_JUDGE_MIN_CHARS and not in_the_exchange:
            return None
        if not GROUP_JOIN_TOPICS and not in_the_exchange:
            # Nobody can be referring to a man who is not in the conversation.
            return None

    if not judge_budget_ok(key):
        log.info("%s | judge budget spent for this minute", key)
        return None

    verdict = should_speak(key, since, name)
    if verdict:
        return verdict
    if verdict == "":
        return None                      # the judge deliberately said no
    # None means the call itself failed. Without a judge the only safe rule is:
    # answer if he was called by name, otherwise keep out of it.
    log.info("%s | judge unavailable, %s", key,
             "answering because a name was used" if name else "staying quiet")
    return f"called {name!r} (judge unavailable)" if name else None


def handle_group_message(msg: dict, cancel: Optional[threading.Event] = None) -> None:
    cancel = cancel if cancel is not None else NEVER_CANCELLED
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    sender = msg.get("from") or {}
    text = (msg.get("text") or msg.get("caption") or "").strip()

    # Every group message is traceable at DEBUG. At INFO this would flood a
    # busy room, but when the bot "does nothing" it is the first thing to look
    # at - set LOG_LEVEL=DEBUG and every message shows up with its verdict.
    log.debug("group %s | %s: %r", chat_id, display_name(sender), text[:60])

    if not text or sender.get("is_bot") or sender.get("id") in IGNORE_USER_IDS:
        return log.debug("  -> skipped: empty, from a bot, or an ignored user")
    if not group_allowed(chat_id):
        return

    # A room that passed the check is a room you are in. That is the set the
    # inline "shares a group with me" test is measured against.
    if chat_id not in known_groups:
        known_groups.add(chat_id)
        save_known_groups()
    # Everyone who talks here demonstrably shares this room with you, so they
    # can use the bot inline without a single membership call.
    allow_inline_for(sender.get("id"), chat_id)

    # A forum topic is its own room: separate people, separate subject, and a
    # reply belongs in the topic it was asked in. So it gets its own transcript
    # - otherwise the judge reads three unrelated conversations as one and the
    # answers cite whatever was said in a topic nobody here is reading. An
    # ordinary group, and the General topic, are unchanged: thread_id is None
    # and the key stays "group:<chat>".
    thread_id = thread_of(msg)
    key = f"group:{chat_id}" + (f":{thread_id}" if thread_id else "")

    # The first message from each forum shows exactly what Telegram sent and
    # what was made of it. "answer went to General instead of the topic" is
    # otherwise invisible from outside, and this line settles it in one look.
    if (chat.get("is_forum") or msg.get("message_thread_id")) \
            and chat_id not in forums_seen:
        forums_seen.add(chat_id)
        log.info("group %s is a forum: is_forum=%s message_thread_id=%s "
                 "is_topic_message=%s -> answering in %s",
                 chat_id, chat.get("is_forum"), msg.get("message_thread_id"),
                 msg.get("is_topic_message"),
                 f"topic {thread_id}" if thread_id else "General")

    # Strip our own @mention so the model doesn't answer its own username.
    clean = text
    if BOT_USERNAME:
        clean = re.sub(rf"@{re.escape(BOT_USERNAME)}\b", "", clean, flags=re.I).strip()

    # Everything said in the room is remembered, so that when we do speak we
    # know what the conversation has been about.
    remember(key, "user", f"{display_name(sender)}: {clean}")

    reason = group_trigger(msg, text, key)
    if not reason:
        log.debug("  -> nothing here for him to answer")
        return

    log.info("group %s%s | %s: %r  (%s)", chat_id,
             f" topic {thread_id}" if thread_id
             else " General" if chat.get("is_forum") else "",
             display_name(sender), clean[:60], reason)

    if REPLY_COOLDOWN and time.time() - last_reply_at.get(key, 0) < REPLY_COOLDOWN:
        log.info("  -> ignored: within REPLY_COOLDOWN")
        return

    log.info("  -> answering...")
    started = time.time()
    answer = ask_ai(key, None, GROUP_NOTE, fast=not GROUP_DELAY, room=True,
                    who=sender,
                    where='the group "{}"{}'.format(
                        chat.get("title") or chat_id,
                        f", topic {thread_id}" if thread_id else ""))
    if not answer:
        log.warning("  -> Gemini returned nothing, no reply sent")
        return

    # No quoting, and no human-typing theatre: in a room full of people a
    # 25-second pause just means the conversation has moved on without you.
    # Quoting only inside a topic, where it doubles as the anchor that keeps
    # the answer out of General.
    anchor = msg.get("message_id") if (thread_id and GROUP_QUOTE_IN_TOPICS) else None

    if GROUP_DELAY:
        if not pace_and_send(None, chat_id, clean, answer, anchor,
                             cancel, time.time() - started, quote=bool(anchor),
                             thread_id=thread_id):
            return
    else:
        send_reply(None, chat_id, answer, anchor, quote=bool(anchor),
                   thread_id=thread_id)
        log.info("  -> replied: %r", answer[:60])

    remember(key, "model", answer)
    compact(key)
    last_reply_at[key] = time.time()
    if not reason.startswith(("mentioned", "replying")):
        last_interjection[key] = time.time()


def dispatch_group_message(msg: dict) -> None:
    """Unlike a 1:1 chat, a newer message here does NOT cancel the answer.

    In a group other people keep talking; that is the normal state of a room,
    not somebody correcting themselves. The chat lock still keeps replies in
    order; REPLY_COOLDOWN would space the replies out, but it is off by
    default so that two people addressing him at once both get an answer.
    """
    chat = msg.get("chat") or {}
    # Per topic, like the transcript: two topics are two conversations and
    # neither should wait on the other's model call.
    thread_id = thread_of(msg)
    key = f"group:{chat.get('id')}" + (f":{thread_id}" if thread_id else "")

    def run() -> None:
        with chat_lock(key):
            try:
                handle_group_message(msg)
            except Exception:
                log.exception("error answering group %s", chat.get("id"))

    run_off_poll_loop(run)


def dispatch_business_message(msg: dict) -> None:
    """Answer in a worker thread, but keep each chat strictly in order.

    A reply now takes ten to twenty seconds of deliberate typing. Doing that on
    the polling thread would freeze every other conversation for the duration.
    """
    chat = msg.get("chat") or {}
    key = f"{msg.get('business_connection_id')}:{chat.get('id')}"

    # Anything still being composed for this chat is now out of date - a newer
    # message has arrived, or this one replaced an edited original.
    cancel_pending(key, "a newer message arrived")

    entry = Pending(msg.get("message_id"))
    with _pending_guard:
        pending[key] = entry

    def run() -> None:
        with chat_lock(key):
            try:
                handle_business_message(msg, entry.cancel)
            except Exception:
                log.exception("error answering chat %s", chat.get("id"))

    run_off_poll_loop(run)


def dm_key(chat_id: int, thread_id: Optional[int] = None) -> str:
    """Where this conversation's transcript lives.

    A thread in the bot's own chat is a separate conversation, exactly as a
    forum topic is, so it gets its own transcript: start a new thread and you
    start clean, go back to an old one and it remembers. /reset empties the
    thread you are in and nothing else.

    With memory off it is the shared, permanently empty transcript - the same
    one inline answers use. Nothing is ever written to it, so it stays empty,
    and switching the setting off cannot leave an old thread behind to leak
    into the next question.
    """
    if not DM_CHAT_MEMORY:
        return INLINE_KEY
    return f"dm:{chat_id}" + (f":{thread_id}" if thread_id else "")


def handle_owner_dm(msg: dict) -> None:
    """You, talking to the bot in its own chat.

    Deliberately not Ilya. The question goes to the model exactly as typed -
    no persona, no house style, no date, no "answer in one line" - which is
    what the inline "Ответить" option does, and what was asked for here. The
    reply is sent the moment it is ready: nobody needs typing theatre from
    their own bot.
    """
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or msg.get("caption") or "").strip()
    thread_id = thread_of(msg)
    key = dm_key(chat_id, thread_id)

    log.info("dm from you%s: %r",
             f" (thread {thread_id})" if thread_id else "", text[:60])
    started = time.time()
    question = text[:MAX_INPUT_CHARS]

    # With memory on the question goes into the transcript and ask_ai reads it
    # from there, so the thread stays in one place; with memory off it is
    # handed over directly and nothing is stored.
    if DM_CHAT_MEMORY:
        remember(key, "user", question)
        question = None

    with Typing(None, chat_id, thread_id):
        # max_chars=0 on purpose: REPLY_MAX_CHARS is a leash on the persona,
        # and there is no persona here. sendMessage splits anything over
        # Telegram's 4096 into several messages by itself.
        answer = ask_ai(key, question, FORMAT_NOTE if DM_CHAT_FORMAT else "",
                        raw=True, max_chars=0,
                        who=msg.get("from"), private=True,
                        where="your own chat with the bot")

    if not answer:
        log.warning("  -> nothing came back")
        send_reply(None, chat_id,
                   "Ни одна модель не ответила. /check покажет, что с провайдерами.",
                   None, thread_id=thread_id)
        return

    send_reply(None, chat_id, answer, None, thread_id=thread_id,
               markdown=DM_CHAT_FORMAT)
    if DM_CHAT_MEMORY:
        remember(key, "model", answer)
        compact(key)
    log.info("  -> answered in %.1fs: %r", time.time() - started, answer[:60])


def dispatch_owner_dm(msg: dict) -> None:
    """Off the poll loop, one at a time per chat - a model call takes seconds
    and the polling thread must not spend them waiting."""
    chat_id = (msg.get("chat") or {}).get("id")
    thread_id = thread_of(msg)

    def run() -> None:
        with chat_lock(f"dm:{chat_id}" + (f":{thread_id}" if thread_id else "")):
            try:
                handle_owner_dm(msg)
            except Exception:
                log.exception("error answering your DM")

    run_off_poll_loop(run)


def probe_provider(p: Provider) -> tuple:
    """One tiny real request. Returns (ok, detail, seconds)."""
    started = time.time()

    if p.name == "gemini":
        try:
            r = session.post(
                gemini_url(GEMINI_MODEL), headers=gemini_headers(p.key),
                json={"contents": [{"role": "user", "parts": [{"text": "Reply with OK"}]}],
                      "generationConfig": {"maxOutputTokens": 1024, "temperature": 0}},
                timeout=25,
            )
        except Exception as exc:
            return False, f"network error: {exc}", time.time() - started
        took = time.time() - started
        return (True, GEMINI_MODEL, took) if r.status_code == 200 else (
            False, http_error(r), took)

    headers = provider_headers(p)
    try:
        r = session.post(
            f"{p.base}/chat/completions", headers=headers,
            json={"model": p.model, "max_tokens": 16, "temperature": 0,
                  "messages": [{"role": "user", "content": "Reply with OK"}]},
            timeout=25,
        )
    except Exception as exc:
        return False, f"network error: {exc}", time.time() - started
    took = time.time() - started
    if r.status_code == 200:
        return True, p.model, took

    detail = http_error(r)
    # A dead model name is the most common failure, and the fix is knowable:
    # ask the provider what it actually serves.
    if looks_like_model_error(r.status_code, r.text):
        candidates = replacement_models(p, r.text)
        if r.status_code == 402:
            candidates.sort(key=model_size)
        failures = []
        saw_payment = r.status_code == 402
        for alt in candidates:
            try:
                r2 = session.post(
                    f"{p.base}/chat/completions", headers=headers,
                    json={"model": alt, "max_tokens": 16, "temperature": 0,
                          "messages": [{"role": "user", "content": "Reply with OK"}]},
                    timeout=25,
                )
            except Exception as exc:
                failures.append((alt, 0, str(exc)[:60]))
                continue
            if r2.status_code == 200:
                dead, p.model = p.model, alt
                log.warning("check: %s switched %s -> %s", p.name, dead, alt)
                return (True,
                        f"{alt}  (was {dead}; pin it with {p.name.upper()}_MODEL)",
                        time.time() - started)
            failures.append((alt, r2.status_code, http_error(r2)))
            if r2.status_code == 402 and not saw_payment:
                saw_payment = True
                # Everything from here on: cheapest first.
                candidates.sort(key=model_size)

        if not failures:
            return False, detail + " - and it lists no usable chat model", took

        codes = {code for _, code, _ in failures}
        # If every alternative is refused the same way, the model was never the
        # problem - say what the real one is instead of listing dead names.
        if codes <= {401, 403}:
            verdict = ("the key itself is being refused (%s) - check it is "
                       "active and has inference permission" % failures[0][2])
        elif codes <= {402}:
            verdict = ("every model this key can see is paid - the free tier "
                       "covers smaller ones, set %s_MODEL by hand from the "
                       "provider's free list" % p.name.upper())
        elif codes == {404} or codes == {404, 410}:
            verdict = ("this key has access to none of them: "
                       + ", ".join(a for a, _, _ in failures[:4]))
        else:
            verdict = "; ".join(f"{a} -> {m}" for a, _, m in failures[:2])
        return False, f"{detail} - tried {len(failures)} alternatives, {verdict}", took
    return False, detail, took


def http_error(r: Any) -> str:
    try:
        err = r.json().get("error", {})
        msg = err.get("message") if isinstance(err, dict) else str(err)
    except Exception:
        msg = None
    msg = (msg or r.text or "")[:120].replace("\n", " ").strip()
    return f"HTTP {r.status_code}: {msg}" if msg else f"HTTP {r.status_code}"


def check_providers() -> str:
    """Probe every configured provider and describe what happened."""
    if not PROVIDERS:
        return "No AI providers configured at all - the bot cannot answer anything."

    lines, working = [], 0
    for p in PROVIDERS:
        ok, detail, took = probe_provider(p)
        if ok:
            p.revive()
            working += 1
            lines.append(f"OK    {p.name:<11} {detail}  ({took:.1f}s)")
        else:
            lines.append(f"FAIL  {p.name:<11} {detail}")
        log.info("check: %s %s", p.name, "ok" if ok else f"failed - {detail}")

    head = f"{working} of {len(PROVIDERS)} providers answered."
    if working == 0:
        head += " Nothing will work until one of them does."
    elif working < len(PROVIDERS):
        head += " The working ones cover for the rest."
    return head + "\n\n" + "\n".join(lines)


def status_report() -> str:
    lines = [
        f"locked to owner: {OWNER_ID or 'NO - anyone can use this bot'}",
        f"group trigger: {GROUP_TRIGGER}"
        + (f" via {judge_model()}" if GROUP_TRIGGER == "context" else "")
        + (", may join topics" if GROUP_JOIN_TOPICS else ", only when addressed"),
        "parked: " + (", ".join(
            f"{p.name} ({int(p.parked_until - time.time())}s)"
            for p in PROVIDERS if p.parked) or "none"),
        "group messages: " + (
            "all visible"
            if BOT_SEES_ALL_GROUP_MESSAGES
            else "PRIVACY MODE ON - only mentions and replies arrive, so he "
                 "cannot see jabs or topics (see /setprivacy in BotFather)"
        ),
        "updates received: " + (", ".join(
            f"{k} {n}" for k, n in sorted(update_counts.items())
            if not k.startswith("_")) or "none yet"),
        "inline: " + (
            "off (INLINE_ENABLED=false)" if not INLINE_ENABLED else
            INLINE_ACCESS
            + ("+dm" if INLINE_ACCESS == "shared" and INLINE_TRUST_DM else "")
            + f", {inline_offered} menus / {inline_spent} "
            f"generated ({inline_sent} sends reported by Telegram)"
            + (f", groups known: {len(known_groups)}, "
               f"people cleared: {sum(1 for v in inline_ok_cache.values() if v[0])}"
               if INLINE_ACCESS == "shared" else "")
        ),
        "memory: " + (
            f"{HISTORY_TURNS} turns verbatim"
            + (f" + notes on what fell out ({sum(1 for v in summaries.values() if v)}"
               f" chat(s) have them, folded {SUMMARY_BATCH} turns at a time)"
               if SUMMARY_ENABLED else ", nothing kept past that")
        ),
        "every mode is told: " + (
            "nothing (RAW_CONTEXT=false)" if not RAW_CONTEXT else
            "the time"
            + (", your profile (" + ", ".join(owner_card) + ")"
               if owner_card else ", no profile filled in")
            + (", your live pin" if owner_location.get("live") else
               ", your pin" if owner_location else
               f", {OWNER_LOCATION}" if OWNER_LOCATION else ", no place")
            + (f", and: {owner_note[:40]}" if owner_note else "")
        ),
        "this chat: " + (
            "off (DM_CHAT_ENABLED=false)" if not DM_CHAT_ENABLED else
            "raw questions, no persona"
            + (", formatted" if DM_CHAT_FORMAT else ", plain text")
            + (", remembering the thread" if DM_CHAT_MEMORY else ", one-shot")
        ),
        "judge (cheapest first): " + (
            ", ".join(f"{c.provider.name}/{c.model}" for c in judge_order()[:3])
            or "nothing usable") + (
            f"   [{sum(1 for c in LADDER if c.bad_judge)} dropped as unable "
            f"to judge]" if any(c.bad_judge for c in LADDER) else ""),
        "ladder (best first):",
    ] + [
        "  {}{:<40} q={}{}".format(
            "   " if c.usable else "x  ",
            f"{c.provider.name}/{c.model}",
            c.quality,
            f" {c.seconds:.0f}s" if c.seconds else "",
        ) + ("" if c.usable else
             "  dead" if c.dead else
             f"  resting {int(c.cool_until - time.time())}s" if c.cool_until > time.time()
             else "  provider parked")
        for c in ladder_order()[:10]
    ] + [
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


# --------------------------------------------------------------------------
# Inline mode - summoning him in a chat he is not a member of
# --------------------------------------------------------------------------
#
# Telegram delivers "@thebot whatever" from ANY chat straight to the bot; it
# does not need to be in that chat, or in any chat. What it does NOT deliver is
# which chat that was. An inline query carries the sender, the text and a coarse
# chat_type - never a chat id - so "only in chats I am in" is not something a
# bot can enforce. What it can check is the person: INLINE_ACCESS=shared serves
# anyone who sits in one of the groups you are in, which is the same guarantee
# from the other side.
#
# You send first, he answers after. Typing costs nothing: every keystroke gets
# the same instant stub back. The model runs once, when you actually send the
# message, and the sent message is then edited into the reply. That is one call
# per message sent - not per message typed, and not per letter.
#
# Telegram only reports which result you picked, and only hands over the
# inline_message_id needed to edit it, if the result carried an inline keyboard.
# So the stub gets a one-button hourglass, and the edit takes it away again.

# user id -> (allowed, checked at). Membership does not change by the minute.
inline_ok_cache: Dict[int, Tuple[bool, float]] = {}

# When each person last summoned him, for INLINE_MAX_PER_MIN.
inline_calls: Dict[int, Deque[float]] = defaultdict(deque)

# Groups the owner is in and the bot can see. The Bot API cannot list a bot's
# chats, so this fills in as people talk - and is persisted, so a restart does
# not lock everyone but you out until somebody happens to write.
known_groups: set = set()

inline_offered = 0     # results handed back with a finished answer in them
inline_spent = 0       # model calls actually made
inline_sent = 0        # sends Telegram happened to report - a sample, not a total
inline_seen = False        # has any inline query ever arrived this run?

# token -> (what was typed, who typed it). The token travels in the result id
# AND in the button's callback_data, so the stub can be filled in from either
# event: chosen_inline_result (which Telegram only samples) or a tap on the
# button (which always arrives). Whichever comes first pops it, so it fills in
# once. The name is stored rather than read off the tap, because anyone in the
# chat can press the button - the line belongs to whoever asked.
inline_pending: Dict[str, Tuple[str, str]] = {}

# Messages already being filled in, so the two routes cannot both pay for the
# same answer - and, more visibly, so the loser of that race does not report
# the winner's work as a stale request.
inline_filling: Dict[str, float] = {}
_fill_guard = threading.Lock()

# The menu you get after typing his name. Only the one you pick costs a call.
# The last field says whether the question goes to the model bare - no persona,
# no instructions, nothing. "Ответить" does: sometimes you want the model, not
# the character.
INLINE_STYLES = (
    ("reply", "Ответить", "напрямую, без персонажа", "", True),
    ("short", "Коротко", "одна фраза, без персонажа",
     "Answer the question in one short sentence. No preamble, no explanation, "
     "no follow-up question, no sign-off. Reply in the language of the "
     "question.", True),
    ("sharp", "Жёстко", "тот же ответ, но с зубами", "\nSharpen it. Be blunt "
     "and funny at the other person's expense, swear if the line you were "
     "given swore first, and never soften the ending. The limits above still "
     "hold: nothing about ethnicity, nationality, religion, gender, "
     "sexuality, disability, illness or family, and no threats.", False),
)



def allow_inline_for(user_id: int, chat_id: int) -> bool:
    """Record that this person is somebody you share a chat with.

    Called for free from two places, and neither one costs an API call because
    the evidence is already in the update: somebody speaking in one of your
    groups, and either side of a 1:1 chat on your business account. The people
    you actually talk to are therefore warm in the cache long before they try
    to summon him. Returns True if this is news.
    """
    if not user_id or user_id == OWNER_ID:
        return False
    if inline_ok_cache.get(user_id, (False, 0))[0]:
        return False                             # already known, don't re-write
    inline_ok_cache[user_id] = (True, time.time() + INLINE_MEMBER_DAYS * 86400)
    save_shared_member(user_id, chat_id)
    return True


def may_use_inline(user_id: Optional[int]) -> bool:
    """Is this person allowed to summon him?

    "shared" means: is there a chat the two of you are both in - any room you
    both sit in, or (with INLINE_TRUST_DM) a 1:1 conversation on your business
    account. A yes is kept for INLINE_MEMBER_DAYS and mirrored into Redis, so
    it survives a restart and the whole group keeps working; a no is kept for
    minutes, so somebody who joins tomorrow is not locked out until next week.

    Both cheap routes fill the cache on their own, before anyone tries to use
    inline at all. This function only does the expensive thing - asking
    Telegram, group by group - for a person neither route has covered.
    """
    if not user_id:
        return False
    if not OWNER_ID or user_id == OWNER_ID or INLINE_ACCESS == "all":
        return True
    if INLINE_ACCESS == "owner":
        return False

    now = time.time()
    cached = inline_ok_cache.get(user_id)
    if cached and now < cached[1]:
        return cached[0]

    # Redis first: another instance, or this one before the last redeploy, may
    # already have paid the API calls for this person.
    seen_in = load_shared_member(user_id)
    if seen_in is not None:
        inline_ok_cache[user_id] = (True, now + INLINE_MEMBER_DAYS * 86400)
        return True

    # Otherwise ask Telegram, one group at a time. First hit wins, so this is
    # normally a single call - and only once a week per person.
    for chat_id in list(known_groups):
        res = tg("getChatMember", chat_id=chat_id, user_id=user_id)
        if res and res.get("status") not in ("left", "kicked"):
            allow_inline_for(user_id, chat_id)
            log.info("inline: %s shares group %s with you - allowed for %d days",
                     user_id, chat_id, INLINE_MEMBER_DAYS)
            return True

    inline_ok_cache[user_id] = (False, now + INLINE_MISS_MINUTES * 60)
    log.info("inline: %s shares no group with you - turned away for %d min",
             user_id, INLINE_MISS_MINUTES)
    return False


def answer_nothing(query_id: str, note: str, seconds: int = 0) -> None:
    """An empty result with a line explaining why - better than a silent bot."""
    tg("answerInlineQuery", inline_query_id=query_id, results=[],
       cache_time=seconds, is_personal=True,
       button={"text": note, "start_parameter": "inline"})


def speaker_name(user: dict) -> str:
    """Their name with the first letter capitalised - and nothing else touched.

    Not .capitalize(), which lowercases the rest and would turn MAX into Max.
    """
    name = display_name(user or {})
    return name[:1].upper() + name[1:]


def wrap_block(escaped: str, style: str) -> str:
    """Set a chunk of already-escaped text off from the rest of the message."""
    if style == "expandable":
        return f"<blockquote expandable>{escaped}</blockquote>"
    if style == "pre":
        return f"<pre>{escaped}</pre>"
    if style == "bold":
        return f"<b>{escaped}</b>"
    if style == "plain":
        return escaped
    return f"<blockquote>{escaped}</blockquote>"


def shrink_to(text: str, room: int) -> str:
    """Cut `text` until its ESCAPED form fits `room`, ending with an ellipsis.

    Escaping is what makes this fiddly: one "<" becomes four characters, so
    cutting by raw length can still overflow.
    """
    if room <= 1:
        return "..."
    cut = text
    while cut and len(html.escape(cut)) + 1 > room:
        cut = cut[:int(len(cut) * 0.9)]
    return cut.rstrip() + "..."


def format_inline(name: str, question: str, body: str) -> tuple:
    """Who asked and what they asked, then the answer. Returns (text, mode).

        | Дмитрий
        | ну и что ты на это скажешь
        Скажу, что вопрос звучит как приглашение на драку.

    The name and the question share one block - they are one utterance, and
    splitting them across two blocks made the message look like a form. The
    answer sits plainly underneath: it is the part being read, so it gets no
    decoration competing with it, and no label announcing a machine wrote it.
    """
    esc_body = html.escape(body)
    if not INLINE_SHOW_QUESTION:
        return wrap_block(esc_body, INLINE_BLOCK_ANSWER), "HTML"

    esc_name = f"<b>{html.escape(name)}</b>"
    # All of it shares one message, and Telegram's 4096-character ceiling is
    # the only thing that shortens anything.
    overhead = (len(esc_name) + 2 + len(wrap_block("", INLINE_BLOCK))
                + len(wrap_block("", INLINE_BLOCK_ANSWER)))
    budget = TELEGRAM_MAX_CHARS - overhead
    esc_q = html.escape(question)

    if len(esc_q) + len(esc_body) > budget:
        # The question keeps up to half the room, plus whatever the answer does
        # not use. So a short question next to a runaway answer is left alone -
        # trimming it there would be cutting the wrong thing.
        q_room = max(budget - len(esc_body), budget // 2)
        if len(esc_q) > q_room:
            esc_q = html.escape(shrink_to(question, q_room))
            log.info("  -> question trimmed to fit Telegram's 4096 limit")
        if len(esc_body) > budget - len(esc_q):
            esc_body = html.escape(shrink_to(body, budget - len(esc_q)))
            log.info("  -> answer trimmed to fit Telegram's 4096 limit")

    quoted = wrap_block(f"{esc_name}\n{esc_q}", INLINE_BLOCK)
    return f"{quoted}\n{wrap_block(esc_body, INLINE_BLOCK_ANSWER)}", "HTML"


def edit_inline(inline_message_id: str, name: str, question: str,
                body: str) -> bool:
    """Replace the stub with the finished message."""
    text, mode = format_inline(name, question, body)
    if tg("editMessageText", inline_message_id=inline_message_id,
          text=text, parse_mode=mode) is not None:
        return True
    # Some character upset the parser. The answer matters more than the styling.
    log.warning("  -> formatted edit refused, retrying as plain text")
    return tg("editMessageText", inline_message_id=inline_message_id,
              text=f"{name}\n{question}\n{body}") is not None


def handle_inline_query(q: dict) -> None:
    """Instant and free: a menu of three. Nothing is generated until you pick."""
    global inline_seen, inline_offered
    query_id = q.get("id")
    sender = q.get("from") or {}
    user_id = sender.get("id")
    text = (q.get("query") or "").strip()

    if not inline_seen:
        inline_seen = True
        log.info("inline query received from %s (%r) - the panel is working",
                 user_id, text[:40])
    else:
        log.debug("inline query from %s: %r", user_id, text[:60])

    if not text:
        return answer_nothing(query_id, "Напиши, на что ответить")
    if not may_use_inline(user_id) or user_id in IGNORE_USER_IDS:
        return answer_nothing(query_id, "This bot is private.", 300)

    token = hashlib.md5(text.encode("utf-8")).hexdigest()[:32]
    inline_pending[token] = (text, speaker_name(sender))
    if len(inline_pending) > 500:
        for k in list(inline_pending)[:250]:
            inline_pending.pop(k, None)

    stub, stub_mode = format_inline(speaker_name(sender), text, INLINE_PLACEHOLDER)
    results = []
    for style_id, title, blurb, _, _raw in INLINE_STYLES:
        results.append({
            "type": "article",
            "id": f"{style_id}:{token}",
            "title": title,
            "description": f"{blurb} — «{text[:60]}»",
            "input_message_content": {"message_text": stub,
                                      "parse_mode": stub_mode},
            # The keyboard earns its place twice: without it Telegram hands
            # back no inline_message_id at all, and tapping it is the reliable
            # way to fill the stub in - the "user chose this" report is only
            # sampled by Telegram, so it cannot be waited on.
            "reply_markup": {"inline_keyboard": [[
                {"text": "⏳ ответить", "callback_data": f"{style_id}:{token}"}]]},
        })

    inline_offered += 1
    tg("answerInlineQuery", inline_query_id=query_id, results=results,
       cache_time=0, is_personal=True)


def split_token(raw: str) -> tuple:
    """"sharp:ab12..." -> ("sharp", "ab12...")"""
    style_id, _, token = (raw or "").partition(":")
    return style_id, token


def handle_chosen_inline_result(chosen: dict) -> None:
    """Telegram reported the send. Nice when it happens - it only samples it."""
    global inline_sent
    inline_sent += 1
    style_id, token = split_token(chosen.get("result_id"))
    sender = chosen.get("from") or {}
    text, name = inline_pending.pop(
        token, ((chosen.get("query") or "").strip(), speaker_name(sender)))
    fill_inline_message(chosen.get("inline_message_id"), text, name, style_id,
                        sender.get("id"))


def handle_callback_query(cb: dict) -> None:
    """The button on a stub. This is the path that always works."""
    inline_message_id = cb.get("inline_message_id")
    style_id, token = split_token(cb.get("data"))
    pending = inline_pending.pop(token, None)

    if pending is None:
        # Either the sampled chosen_inline_result beat us to it and is already
        # generating, or this really is an old button from before a restart.
        # Either way the message itself is left alone: overwriting somebody
        # else's answer-in-progress with "expired" is exactly the wrong move.
        busy = inline_message_id in inline_filling
        note = "Уже отвечаю." if busy else "Запрос протух - набери заново."
        tg("answerCallbackQuery", callback_query_id=cb.get("id"), text=note)
        return

    tg("answerCallbackQuery", callback_query_id=cb.get("id"), text="Секунду.")
    text, name = pending
    fill_inline_message(inline_message_id, text, name, style_id,
                        (cb.get("from") or {}).get("id"))


def fill_inline_message(inline_message_id: Optional[str], text: str,
                        name: str, style_id: str,
                        user_id: Optional[int]) -> None:
    """Run the model and turn the posted placeholder into the reply."""
    if not may_use_inline(user_id) or user_id in IGNORE_USER_IDS:
        return
    if not inline_message_id or not text:
        return

    # Both routes can fire for the same message. Whoever claims it answers; the
    # other one leaves quietly instead of paying for a second identical call.
    with _fill_guard:
        if inline_message_id in inline_filling:
            log.debug("  -> %s is already being filled in", inline_message_id)
            return
        inline_filling[inline_message_id] = time.time()
        if len(inline_filling) > 300:
            for k in sorted(inline_filling, key=inline_filling.get)[:150]:
                inline_filling.pop(k, None)

    if not rate_ok(inline_calls, user_id, INLINE_MAX_PER_MIN):
        log.info("inline: %s is over %d/min", user_id, INLINE_MAX_PER_MIN)
        edit_inline(inline_message_id, name, text, "Слишком часто. Подожди минуту.")
        return

    extra, raw = next(((e, r) for sid, _, _, e, r in INLINE_STYLES
                       if sid == style_id), ("", False))
    log.info("inline %s%s from %s: %r", style_id or "reply",
             " (raw, no persona)" if raw else "", user_id, text[:60])
    started = time.time()
    global inline_spent
    inline_spent += 1
    # Same rules as a room he actually sits in - minus the parts that assume he
    # can see it. One message, so 4096 is a real ceiling, and the quote above
    # the answer eats into it.
    # The quote and the answer share one message. Leave the answer a floor:
    # without it a very long question drives this negative, and a non-positive
    # limit means "do not trim at all" - the opposite of what is wanted.
    # In raw mode `extra` IS the whole system prompt - INLINE_NOTE is part of
    # the character, and the character is exactly what raw leaves out.
    answer = ask_ai(INLINE_KEY, text[:MAX_INPUT_CHARS],
                    extra if raw else INLINE_NOTE + extra,
                    fast=True, room=True, raw=raw,
                    who={"id": user_id, "first_name": name},
                    # Only your own summon is private. Somebody else's inline
                    # question is read by a chat you cannot even see.
                    private=bool(OWNER_ID) and user_id == OWNER_ID,
                    where="some other chat - Telegram does not say which",
                    max_chars=max(600, TELEGRAM_MAX_CHARS - len(text) - 200))

    if not answer:
        log.warning("  -> nothing came back")
        edit_inline(inline_message_id, name, text, "Не сейчас.")
        return
    if not edit_inline(inline_message_id, name, text, answer):
        log.warning("  -> could not fill in the message")
        return
    log.info("  -> inline answer in %.1fs: %r", time.time() - started, answer[:60])


def dispatch_inline(handler, payload: dict) -> None:
    def run() -> None:
        try:
            handler(payload)
        except Exception:
            log.exception("error answering an inline query")

    run_off_poll_loop(run)


# What Telegram has actually delivered this run, by kind. The single most
# useful thing when something "does not work": it separates "the update never
# arrived" from "it arrived and the bot mishandled it", which otherwise look
# exactly the same from outside.
update_counts: Dict[str, int] = defaultdict(int)

FIRST_TIME_WORTH_SAYING = {
    "chosen_inline_result": "Telegram sampled one of your sends - it only "
                            "reports a fraction of them, which is why nothing "
                            "depends on this update any more",
    "business_message": "Telegram Business is wired up",
}


def handle_update(update: dict) -> None:
    kind = next((k for k in update if k != "update_id"), "empty")
    update_counts[kind] += 1
    if update_counts[kind] == 1 and kind in FIRST_TIME_WORTH_SAYING:
        log.info("first %s of this run - %s", kind, FIRST_TIME_WORTH_SAYING[kind])
    else:
        log.debug("update %s: %s", update.get("update_id"), kind)

    if "inline_query" in update:
        if INLINE_ENABLED:
            handle_inline_query(update["inline_query"])
        return
    if "chosen_inline_result" in update:
        if INLINE_ENABLED:
            dispatch_inline(handle_chosen_inline_result, update["chosen_inline_result"])
        return
    if "callback_query" in update:
        if INLINE_ENABLED:
            dispatch_inline(handle_callback_query, update["callback_query"])
        return
    if "edited_message" in update:
        # The only edit worth reading: a live location moving. Everything else
        # in the bot's own chat is you fixing a typo, which needs no answer.
        edited = update["edited_message"]
        if (not OWNER_ID or (edited.get("from") or {}).get("id") == OWNER_ID) \
                and remember_location(edited):
            log.info("live location moved to %s, %s",
                     owner_location["lat"], owner_location["lon"])
        return

    if "business_connection" in update:
        remember_connection(update["business_connection"])
        return
    if "business_message" in update:
        dispatch_business_message(update["business_message"])
        return
    if "edited_business_message" in update:
        msg = update["edited_business_message"]
        chat = msg.get("chat") or {}
        key = f"{msg.get('business_connection_id')}:{chat.get('id')}"
        with _pending_guard:
            p = pending.get(key)
            mid = msg.get("message_id")
            answering_it = bool(p and p.message_id == mid)
        if answering_it:
            # They fixed it before we answered - throw our draft away and read
            # the new version from scratch.
            log.info("message %s edited mid-answer, starting over", mid)
            dispatch_business_message(msg)
        else:
            log.info("edited business message %s, already answered - ignored", mid)
        return

    if "deleted_business_messages" in update:
        d = update["deleted_business_messages"]
        chat = d.get("chat") or {}
        key = f"{d.get('business_connection_id')}:{chat.get('id')}"
        ids = set(d.get("message_ids") or [])
        with _pending_guard:
            p = pending.get(key)
            mine = p.message_id if p else None
        if mine in ids:
            # They took it back before we answered. Say nothing and wait for
            # whatever they write next.
            log.info("message %s deleted before the reply went out", mine)
            cancel_pending(key, "the message was deleted", only_message_id=mine)
        return

    msg = update.get("message")
    if not msg:
        log.info("update ignored (%s)", ", ".join(k for k in update if k != "update_id"))
        return

    # Group chats: the bot is a plain member here, not a business assistant.
    if (msg.get("chat") or {}).get("type") in ("group", "supergroup"):
        if GROUPS_ENABLED:
            dispatch_group_message(msg)
        return

    # A DM to the bot itself: commands, and - for you - an ordinary chat.
    text = (msg.get("text") or msg.get("caption") or "").strip()
    sender_id = (msg.get("from") or {}).get("id")
    is_owner = not OWNER_ID or sender_id == OWNER_ID

    chat_id = msg["chat"]["id"]
    # The bot's own chat has threads too. Every line the bot writes here -
    # answers, command output, the refusal below - goes back into the thread
    # it was asked in, or it turns up in the main flow where nobody is looking.
    thread_id = thread_of(msg)

    def say(text_out: str) -> None:
        send_reply(None, chat_id, text_out, None, thread_id=thread_id)

    if not is_owner:
        # Somebody else found the bot. Don't hand them diagnostics.
        log.info("DM from %s (not the owner), turned away", sender_id)
        if text.startswith("/"):
            say("This bot is private.")
        return

    # A pin you dropped. Telegram never volunteers where you are, so this is
    # the only way the raw modes can know - and it is worth a word back, or you
    # cannot tell whether it landed.
    if remember_location(msg):
        say("Записал: {}, {}{}. Держится {} ч - потом снова буду знать только "
            "время.".format(owner_location["lat"], owner_location["lon"],
                            " (live)" if owner_location["live"] else "",
                            LOCATION_TTL_HOURS))
        return

    if text.startswith("/ctx"):
        global owner_note
        rest = text[4:].strip()
        if rest in ("-", "off", "clear", "стоп"):
            owner_note = ""
            save_context()
            say("Заметку убрал.")
        elif rest:
            owner_note = rest[:400]
            save_context()
            say(f"Буду держать в виду: {owner_note}")
        else:
            say(f"Сейчас держу в виду: {owner_note}" if owner_note else
                "Пусто. /ctx <текст> - что мне держать в виду в каждом ответе "
                "(город, чем занят, какой ноутбук). /ctx - убрать.")
    elif text.startswith("/memory"):
        key = dm_key(chat_id, thread_id)
        recap = summaries.get(key, "")
        say(f"Дословно помню {len(history[key])} реплик.\n\n"
            + (f"Конспект того, что уже вышло из окна:\n{recap}"
               if recap else "Конспекта пока нет - окно ещё не переполнялось."))
    elif text.startswith("/where"):
        block = context_block(msg.get("from"), private=True,
                              where="your own chat with the bot")
        public = context_block(msg.get("from"), private=False,
                               where='the group "..."')
        say(block + "\n\n--- а в группе и в клиентском чате то же самое без "
            "последних строк:\n\n" + public if block else
            "RAW_CONTEXT выключен - в модель ничего из этого не уходит.")
    elif text.startswith("/check"):
        say(f"Checking {len(PROVIDERS)} provider(s), one real request each...")

        def run_check() -> None:
            try:
                say(check_providers())
            except Exception:
                log.exception("provider check failed")
                say("The check itself broke - see the log.")

        # Probing five providers can take a minute; never block the poll loop.
        run_off_poll_loop(run_check)
    elif text.startswith("/status"):
        say(status_report())
    elif text.startswith("/reset"):
        key = dm_key(chat_id, thread_id)
        had = len(history[key]) + (1 if summaries.get(key) else 0)
        history[key].clear()
        summaries.pop(key, None)
        history_seen[key] = time.time()
        with _history_lock:
            dirty_keys.add(key)
        history_dirty.set()
        say(f"Забыл {had} реплик. Дальше с чистого листа."
            if had else "И так пусто.")
    elif text.startswith("/start"):
        say(
            "I'm alive. Connect me under Settings -> Telegram Business -> "
            "Chatbots and I'll answer your chats for you.\n\n"
            f"In any other chat - even one I'm not in - type "
            f"'@{BOT_USERNAME} ' and the line you want answered. The reply "
            "appears above the input box; tap it to send it as your own "
            "message.\n\n"
            "Write to me here and I'll answer you directly - plain assistant, "
            "no character. Each thread in this chat is its own conversation.\n\n"
            "/status - what I currently know\n"
            "/check  - test every AI provider key\n"
            "/reset  - forget this thread\n"
            "/memory - what I remember here, word for word and in note form\n"
            "/ctx    - a line about you I keep in mind every time\n"
            "/where  - exactly what I tell the model about the here and now\n\n"
            "Share a location (paperclip -> Location) and I will use it until "
            "it goes stale. A live location keeps itself up to date."
        )
    elif text and DM_CHAT_ENABLED and not text.startswith("/"):
        # Anything that isn't a command: you are talking to him, so answer.
        # Stale questions are not worth answering - on a host that sleeps,
        # Telegram delivers everything it queued the moment the bot wakes up.
        age = time.time() - float(msg.get("date") or 0) if msg.get("date") else 0.0
        if age > MAX_MESSAGE_AGE:
            log.info("dm %.0f min old, older than MAX_MESSAGE_AGE - ignored", age / 60)
            return
        dispatch_owner_dm(msg)


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

# Telegram allows exactly one getUpdates consumer per token. A second one -
# the previous Render deploy still shutting down, a forgotten local run, a
# duplicated service - takes the updates away from this one, which then sees
# nothing at all: no messages, no errors, nothing in the log to explain it.
# That is worth saying out loud, once, instead of a warning every three
# seconds that reads like a network hiccup.
_conflict_since = 0.0


def poll_updates(offset: int) -> Optional[list]:
    """One getUpdates, with the "somebody else is polling" case spelled out."""
    global _conflict_since
    try:
        r = session.post(
            f"{TELEGRAM_API}/getUpdates", timeout=70,
            json={"offset": offset, "timeout": 50,
                  "allowed_updates": ALLOWED_UPDATES},
        )
        data = r.json()
    except Exception as exc:
        log.warning("getUpdates failed: %s", exc)
        return None

    if data.get("ok"):
        if _conflict_since:
            log.warning("the other instance is gone after %.0fs - this one is "
                        "receiving messages again", time.time() - _conflict_since)
            _conflict_since = 0.0
        return data.get("result") or []

    description = str(data.get("description") or "")
    if "conflict" in description.lower():
        if not _conflict_since:
            _conflict_since = time.time()
            update_counts["_conflict"] += 1
            log.error(
                "ANOTHER INSTANCE OF THIS BOT IS RUNNING. Telegram delivers "
                "each message to ONE poller, and it is not this one - which is "
                "why nothing appears here however much you write. Usually the "
                "previous deploy that has not shut down yet (give it a minute), "
                "a second Render service on the same TELEGRAM_BOT_TOKEN, or a "
                "copy still running on your laptop. Two bots on one token can "
                "never both work; stop one."
            )
        else:
            # Already said it. Keep the log readable while it resolves itself.
            log.info("still waiting for the other instance to stop (%.0fs)",
                     time.time() - _conflict_since)
        time.sleep(10)
        return None

    log.warning("getUpdates error: %s", description)
    return None


ALLOWED_UPDATES = [
    "message",
    # Live locations arrive as edits to the message that started them - this
    # is the only way to follow a pin as it moves.
    "edited_message",
    "business_connection",
    "business_message",
    "edited_business_message",
    "deleted_business_messages",
    # Inline mode. chosen_inline_result fills the stub in when Telegram happens
    # to report the send - it only samples those - and callback_query does it
    # when you tap the button, which always arrives.
    "inline_query",
    "chosen_inline_result",
    "callback_query",
]


def main() -> None:
    # Open the port first: hosts that scan for a listening socket (Render free
    # web services) mark the deploy as failed if nothing binds quickly.
    start_health_server()

    if not TELEGRAM_TOKEN:
        sys.exit("Set TELEGRAM_BOT_TOKEN (see .env.example).")
    if not PROVIDERS:
        sys.exit(
            "No AI provider configured. Set at least one of GEMINI_API_KEY, "
            "GROQ_API_KEY, CEREBRAS_API_KEY, OPENROUTER_API_KEY, "
            "MISTRAL_API_KEY, GITHUB_MODELS_TOKEN (see .env.example)."
        )
    log.info("AI providers, in order: %s",
             ", ".join(f"{p.name}/{p.model or 'auto'}" for p in PROVIDERS))

    global BOT_ID, BOT_USERNAME, BOT_SEES_ALL_GROUP_MESSAGES
    me = tg("getMe")
    if not me:
        sys.exit("Telegram rejected the token. Check TELEGRAM_BOT_TOKEN.")
    BOT_ID = me.get("id")
    BOT_USERNAME = me.get("username") or ""
    log.info("logged in as @%s (id %s)", BOT_USERNAME, BOT_ID)

    if OWNER_ID:
        log.info("locked to owner %s - every other account is ignored", OWNER_ID)
    else:
        log.warning(
            "OWNER_ID is not set: anyone who knows @%s can attach it to their "
            "own business account and spend your Gemini quota. Set OWNER_ID to "
            "your Telegram user id.", BOT_USERNAME,
        )
    if GROUPS_ENABLED:
        log.info("group trigger mode: %s", GROUP_TRIGGER)
        # Telegram only delivers ordinary group messages to a bot whose privacy
        # mode is off. Without that the bot literally never sees the text, so
        # keyword triggers and reply-all cannot fire - and nothing appears in
        # the log to explain why.
        sees_everything = bool(me.get("can_read_all_group_messages"))
        BOT_SEES_ALL_GROUP_MESSAGES = sees_everything
        log.info(
            "groups: on, replying when @%s is mentioned, replied to, called by "
            "name, or talked about%s", BOT_USERNAME,
            "; and to every message (reply-all)" if GROUP_REPLY_ALL
            else "; may also join topics" if GROUP_JOIN_TOPICS else "",
        )
        if not sees_everything:
            log.warning(
                "PRIVACY MODE IS ON: Telegram is not delivering ordinary group "
                "messages to this bot, so keyword triggers will never fire. "
                "Fix: @BotFather -> /setprivacy -> @%s -> Disable, then REMOVE "
                "the bot from the group and add it back (the setting is only "
                "applied when it joins).", BOT_USERNAME,
            )
        else:
            log.info("privacy mode off - all group messages are visible")

    if INLINE_ENABLED:
        if INLINE_ACCESS not in ("owner", "shared", "all"):
            log.warning("INLINE_ACCESS=%r is not owner|shared|all - treating it "
                        "as 'owner'", INLINE_ACCESS)
        # An allowlist is a definitive answer to "which groups are yours", so
        # strangers can be judged against it from the first second. Anything
        # learned in an earlier run is remembered too, so a restart does not
        # lock everyone but you out.
        known_groups.update(GROUP_ALLOWLIST)
        load_known_groups()
        load_context()
        if me.get("supports_inline_queries"):
            log.info(
                "inline mode on (access: %s) - type '@%s ...' in ANY chat, even "
                "one this bot was never added to", INLINE_ACCESS, BOT_USERNAME,
            )
            if INLINE_ACCESS == "shared" and not known_groups:
                log.info(
                    "  no groups known yet, so only you can use it inline until "
                    "somebody writes in one of your groups (or set "
                    "GROUP_ALLOWLIST to name them up front)"
                )
            elif INLINE_ACCESS == "all":
                log.warning(
                    "  INLINE_ACCESS=all: anyone who knows @%s can spend your "
                    "quota, capped only by INLINE_MAX_PER_MIN=%d per person",
                    BOT_USERNAME, INLINE_MAX_PER_MIN,
                )
        else:
            log.warning(
                "INLINE MODE IS OFF: typing '@%s ...' in another chat will find "
                "nothing. Fix: @BotFather -> /setinline -> @%s -> send a "
                "placeholder line like 'что ответить...'.",
                BOT_USERNAME, BOT_USERNAME,
            )

    if any(p.name == "gemini" for p in PROVIDERS):
        pick_working_model()
    build_ladder()

    # Only now does the ladder exist, so this is the first point at which the
    # judge's rung can be named or GROUP_JUDGE_MODEL can be checked at all.
    if GROUPS_ENABLED and GROUP_TRIGGER == "context":
        log.info("group judge starts on %s and climbs only if it has to",
                 judge_model())
        if GROUP_JUDGE_MODEL and not any(c.model == GROUP_JUDGE_MODEL for c in LADDER):
            log.warning(
                "GROUP_JUDGE_MODEL=%r is not on the ladder, so it is ignored "
                "and the judge just starts at the top. Check the spelling "
                "against the ladder printed above.", GROUP_JUDGE_MODEL,
            )

    if bool(UPSTASH_URL) != bool(UPSTASH_TOKEN):
        log.warning(
            "only half of the Upstash credentials are set (%s missing) - "
            "falling back to the file, which does not survive a redeploy",
            "UPSTASH_REDIS_REST_TOKEN" if UPSTASH_URL else "UPSTASH_REDIS_REST_URL",
        )

    load_history()
    if redis_on() or HISTORY_FILE:
        threading.Thread(target=history_saver, daemon=True).start()
        where = f"redis ({UPSTASH_URL.split('//')[-1]})" if redis_on() else HISTORY_FILE
        log.info("history persisted to %s every %ds", where, HISTORY_SAVE_EVERY)
        # Render and Docker stop a container with SIGTERM; write before dying.
        signal.signal(signal.SIGTERM, lambda *_: (save_history(), sys.exit(0)))

    # Keep the backlog. On a host that sleeps, the messages that arrived while
    # the instance was down are queued here - dropping them means the customer
    # is silently ignored. MAX_MESSAGE_AGE filters out anything truly stale.
    tg("deleteWebhook", drop_pending_updates=False)
    offset = 0

    log.info("polling for business messages...")
    while True:
        updates = poll_updates(offset)
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
        save_history()
        log.info("bye")

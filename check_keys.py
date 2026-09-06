#!/usr/bin/env python3
"""Check every configured AI provider with one real request.

    python check_keys.py

Reads the same environment variables as bot.py and reports, per provider,
whether the key works, which model answered, and how fast. If the configured
model is rejected, it asks the provider what it does have and retries once.
"""

import json
import os
import sys
import time

import requests

PROVIDERS = [
    # name,        base url,                                key env,               model env,          default model
    ("groq",       "https://api.groq.com/openai/v1",        "GROQ_API_KEY",        "GROQ_MODEL",       "llama-3.3-70b-versatile"),
    ("cerebras",   "https://api.cerebras.ai/v1",            "CEREBRAS_API_KEY",    "CEREBRAS_MODEL",   "llama-3.3-70b"),
    ("openrouter", "https://openrouter.ai/api/v1",          "OPENROUTER_API_KEY",  "OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free"),
    ("mistral",    "https://api.mistral.ai/v1",             "MISTRAL_API_KEY",     "MISTRAL_MODEL",    "mistral-small-latest"),
    ("github",     "https://models.inference.ai.azure.com", "GITHUB_MODELS_TOKEN", "GITHUB_MODEL",     "gpt-4o-mini"),
]

PROMPT = "Reply with exactly one word: OK"
TIMEOUT = 30


def mask(key: str) -> str:
    return f"{key[:6]}…{key[-4:]}" if len(key) > 12 else "…"


def headers(name: str, key: str) -> dict:
    h = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if name == "openrouter":
        h["HTTP-Referer"] = "https://t.me"
        h["X-Title"] = "telegram-business-bot"
    return h


def list_models(name: str, base: str, key: str) -> list:
    try:
        r = requests.get(f"{base}/models", headers=headers(name, key), timeout=TIMEOUT)
        if r.status_code != 200:
            return []
        data = r.json().get("data") or []
        return [m.get("id") for m in data if m.get("id")]
    except Exception:
        return []


def chat(name: str, base: str, key: str, model: str):
    body = {
        "model": model,
        "messages": [{"role": "user", "content": PROMPT}],
        "max_tokens": 20,
        "temperature": 0,
    }
    started = time.time()
    try:
        r = requests.post(f"{base}/chat/completions", headers=headers(name, key),
                          json=body, timeout=TIMEOUT)
    except Exception as exc:
        return None, f"network error: {exc}", 0.0
    took = time.time() - started
    if r.status_code == 200:
        try:
            return r.json()["choices"][0]["message"]["content"].strip(), None, took
        except Exception:
            return None, f"unexpected body: {r.text[:120]}", took
    try:
        detail = r.json().get("error", {})
        detail = detail.get("message") or json.dumps(detail)[:160]
    except Exception:
        detail = r.text[:160]
    return None, f"HTTP {r.status_code}: {detail}".replace("\n", " "), took


def main() -> int:
    print(f"{'provider':<11} {'key':<14} {'status':<8} model / detail")
    print("-" * 78)
    working = 0
    configured = 0

    for name, base, key_env, model_env, default_model in PROVIDERS:
        key = os.environ.get(key_env, "").strip()
        if not key:
            print(f"{name:<11} {'—':<14} {'skipped':<8} no {key_env} set")
            continue
        configured += 1
        model = os.environ.get(model_env, default_model).strip()

        text, err, took = chat(name, base, key, model)
        if text:
            working += 1
            print(f"{name:<11} {mask(key):<14} {'OK':<8} {model}  ({took:.1f}s, said {text[:20]!r})")
            continue

        # Model rejected? Find out what this key can actually use.
        looks_like_model_problem = err and any(
            w in err.lower() for w in ("model", "not found", "404", "decommission")
        )
        if looks_like_model_problem:
            available = list_models(name, base, key)
            if available:
                alt = next((m for m in available if "llama" in m.lower()), available[0])
                text, err2, took = chat(name, base, key, alt)
                if text:
                    working += 1
                    print(f"{name:<11} {mask(key):<14} {'OK*':<8} {alt}  ({took:.1f}s)")
                    print(f"{'':<11} {'':<14} {'':<8} -> set {model_env}={alt}  "
                          f"({model} was rejected)")
                    continue
                err = err2 or err
        print(f"{name:<11} {mask(key):<14} {'FAIL':<8} {err}")

    print("-" * 78)
    print(f"{working} of {configured} configured providers answered.")
    return 0 if working == configured else 1


if __name__ == "__main__":
    sys.exit(main())

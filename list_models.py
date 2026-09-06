#!/usr/bin/env python3
"""Print the Gemini models your API key can actually use."""
import os
import sys

import requests

key = os.environ.get("GEMINI_API_KEY", "").strip()
if not key:
    sys.exit("Set GEMINI_API_KEY first.")

r = requests.get(
    "https://generativelanguage.googleapis.com/v1beta/models",
    headers={"x-goog-api-key": key},
    timeout=30,
)
r.raise_for_status()

for m in r.json().get("models", []):
    if "generateContent" in m.get("supportedGenerationMethods", []):
        print(f"{m['name'].split('/')[-1]:<40} {m.get('displayName', '')}")

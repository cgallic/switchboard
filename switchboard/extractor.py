"""switchboard/extractor.py — transcript -> structured job record.

This is the reasoning step. Given the messy spoken transcript of a phone call,
Claude emits a strict JSON record with exactly seven fields. When ANTHROPIC_API_KEY
is present we call Claude; otherwise we fall back to a deterministic offline
extractor so the whole agent runs (and the demo + tests pass) with no keys.

The record shape is the contract the Notion MCP server files:

    {
      "customer_name": str,
      "phone":         str,
      "service_type":  str,
      "urgency":       "emergency" | "soon" | "routine",
      "address":       str,
      "job_summary":   str,         # one clean sentence
      "quote_hint":    str          # "" if the caller didn't mention budget
    }
"""
from __future__ import annotations

import json
import os
import re
from typing import Optional

URGENCIES = ("emergency", "soon", "routine")

REQUIRED_FIELDS = (
    "customer_name", "phone", "service_type",
    "urgency", "address", "job_summary", "quote_hint",
)

SYSTEM_PROMPT = """You are Switchboard's intake extractor for a service business.
You are given the transcript of a phone call from a customer. Return ONLY a JSON
object with exactly these keys: customer_name, phone, service_type, urgency,
address, job_summary, quote_hint.

Rules:
- urgency is exactly one of: "emergency", "soon", "routine".
  "emergency" = words like leaking, flooding, no heat, today, right now, ASAP.
  "soon"      = this week / in the next few days.
  "routine"   = no time pressure / "whenever".
- job_summary is ONE clean professional sentence a dispatcher could read.
- quote_hint is "" unless the caller named a budget or asked about price.
- If a field is unknown, use "" (empty string). Never invent an address or name.
- Output JSON only. No prose, no code fences."""


def _validate(record: dict) -> dict:
    """Coerce to the strict contract; raise on anything unfixable."""
    if not isinstance(record, dict):
        raise ValueError("extractor did not return an object")
    out = {}
    for k in REQUIRED_FIELDS:
        v = record.get(k, "")
        out[k] = ("" if v is None else str(v)).strip()
    if out["urgency"] not in URGENCIES:
        out["urgency"] = "routine"
    if not out["customer_name"] and not out["phone"]:
        raise ValueError("record has neither a name nor a phone — unusable")
    return out


# --------------------------------------------------------------------------- #
# Offline deterministic extractor (no keys) — keeps the demo + tests runnable
# --------------------------------------------------------------------------- #

_PHONE_RE = re.compile(r"(\+?\d[\d\-\.\s\(\)]{7,}\d)")
_EMERGENCY = ("leak", "leaking", "flood", "flooding", "no heat", "no hot water",
              "burst", "today", "right now", "asap", "emergency", "everywhere")
_SOON = ("this week", "few days", "couple days", "tomorrow", "soon")

_SERVICE_HINTS = {
    "water heater": "Water heater repair",
    "heater": "Water heater repair",
    "leak": "Leak repair",
    "drain": "Drain cleaning",
    "toilet": "Toilet repair",
    "ac": "AC repair",
    "furnace": "Furnace repair",
    "lawn": "Lawn service",
    "tree": "Tree service",
    "clean": "Cleaning",
}


def _offline_extract(transcript: str, caller: Optional[str]) -> dict:
    t = transcript.lower()

    phone = caller or ""
    if not phone:
        m = _PHONE_RE.search(transcript)
        phone = m.group(1).strip() if m else ""

    # name: look for "this is X" / "my name is X" / "I'm X"
    name = ""
    for pat in (r"my name is ([a-z][a-z\s\.']{1,30})",
                r"this is ([a-z][a-z\s\.']{1,30})",
                r"i'?m ([a-z][a-z\s\.']{1,30})"):
        m = re.search(pat, t)
        if m:
            name = m.group(1).strip().title()
            # stop at filler words
            name = re.split(r"\b(and|i|with|from|on|at|calling)\b", name)[0].strip()
            if name:
                break

    # service type
    service = ""
    for hint, label in _SERVICE_HINTS.items():
        if hint in t:
            service = label
            break
    if not service:
        service = "General service request"

    # urgency
    if any(w in t for w in _EMERGENCY):
        urgency = "emergency"
    elif any(w in t for w in _SOON):
        urgency = "soon"
    else:
        urgency = "routine"

    # address: "on Maple", "the blue house", street-ish token
    address = ""
    m = re.search(r"\b(?:on|at)\s+([a-z0-9][a-z0-9\s\.]{2,40}?)(?:\,|\.|\bstreet\b|\bst\b|\bave\b|$)", t)
    if m:
        address = m.group(1).strip().title()

    # quote hint
    quote = ""
    m = re.search(r"\$?\s?(\d{2,5})\s?(?:dollars|bucks|budget)?", t)
    if "price" in t or "cost" in t or "how much" in t or "budget" in t or "$" in transcript:
        quote = (m.group(0).strip() if m else "asked about price")

    # one-sentence summary
    first = transcript.strip().split(".")[0].strip()
    summary = (first[:140] + "...") if len(first) > 140 else first
    if not summary:
        summary = f"{service} requested by {name or 'caller'}."

    return {
        "customer_name": name,
        "phone": phone,
        "service_type": service,
        "urgency": urgency,
        "address": address,
        "job_summary": summary,
        "quote_hint": quote,
    }


# --------------------------------------------------------------------------- #
# Claude extractor (when a key is present)
# --------------------------------------------------------------------------- #

def _claude_extract(transcript: str, caller: Optional[str]) -> dict:
    import anthropic  # imported lazily so offline mode needs no install

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    model = os.environ.get("SWITCHBOARD_MODEL", "claude-3-5-sonnet-latest")
    user = transcript
    if caller:
        user = f"[caller id: {caller}]\n{transcript}"
    msg = client.messages.create(
        model=model,
        max_tokens=600,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    text = text.strip()
    # tolerate accidental code fences
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    return json.loads(text)


def extract_record(transcript: str, caller: Optional[str] = None, *, force_offline: bool = False) -> dict:
    """Extract a strict job record from a call transcript.

    Uses Claude when ANTHROPIC_API_KEY is set and force_offline is False;
    otherwise uses the deterministic offline extractor. Always returns a record
    validated against the seven-field contract.
    """
    use_claude = (not force_offline) and bool(os.environ.get("ANTHROPIC_API_KEY"))
    raw = _claude_extract(transcript, caller) if use_claude else _offline_extract(transcript, caller)
    return _validate(raw)

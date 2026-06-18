"""switchboard/adapters.py — turn ANY inbound into one call-ended payload.

The agent only ever sees a single normalized shape:

    Payload(caller, transcript, source)

Everything upstream of the lane is an *adapter* that produces that payload. The
live phone call is one source; an email and a web form are two more. Because the
agent and the Notion Intake MCP server both operate on the normalized payload,
"any inbound" stops being a claim and becomes the same code path exercised three
ways (a unit test drives all three through the identical lane in tests.py).

This is the file that makes Switchboard plural. Add a new source = add one
adapter function that returns a Payload; the gated DAG, the dedupe, the Notion
write, and the follow-up are unchanged.

    from_call(caller, transcript)        -> Payload   (live voice secretary layer)
    from_form(form_json)                 -> Payload   (website "request service" form)
    from_email(raw_email)                -> Payload   (inbox -> intake)

Each returns the same Payload the agent ingests, so a form submission and an
email land the same filed Notion record a phone call does.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from email import message_from_string
from email.message import Message
from typing import Optional, Union

CALL = "call"
FORM = "form"
EMAIL = "email"


@dataclass
class Payload:
    """The one shape the agent ingests, regardless of where the work came from."""

    caller: Optional[str]      # phone id when we have one (call/form); else ""
    transcript: str            # the text the extractor reasons over
    source: str                # "call" | "form" | "email" — provenance only

    def as_kwargs(self) -> dict:
        return {"caller": self.caller, "transcript": self.transcript}


# --------------------------------------------------------------------------- #
# Source 1 — the live phone call (the voice secretary layer's call-ended payload)
# --------------------------------------------------------------------------- #

def from_call(caller: Optional[str], transcript: str) -> Payload:
    """A finished phone call: caller id + the spoken transcript, as received from
    the voice secretary layer at the call-ended webhook."""
    return Payload(caller=caller or "", transcript=transcript or "", source=CALL)


# --------------------------------------------------------------------------- #
# Source 2 — a website "request service" form (structured JSON in, same record out)
# --------------------------------------------------------------------------- #

# Accept common field aliases so a real-world form schema needs no mapping code.
_FORM_NAME = ("name", "customer_name", "full_name", "your_name", "contact_name")
_FORM_PHONE = ("phone", "phone_number", "tel", "mobile", "contact_phone")
_FORM_BODY = ("message", "details", "description", "notes", "comments",
              "what_do_you_need", "issue", "request")
_FORM_SERVICE = ("service", "service_type", "category", "job_type")


def _first(d: dict, keys: tuple[str, ...]) -> str:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def from_form(form_json: Union[str, dict]) -> Payload:
    """A website service-request form submission.

    The form fields are flattened into a single transcript string the *same*
    extractor reads — so a form produces the same seven-field record a call does,
    with zero changes to the lane. A phone number in the form becomes the caller
    id, which means dedupe works across channels (a caller who later fills the
    form links to their existing job, not a duplicate).
    """
    data = json.loads(form_json) if isinstance(form_json, str) else dict(form_json)
    name = _first(data, _FORM_NAME)
    phone = _first(data, _FORM_PHONE)
    service = _first(data, _FORM_SERVICE)
    body = _first(data, _FORM_BODY)

    # Compose a transcript that reads like the call the extractor was tuned on,
    # so name/service/urgency/address all parse out of one consistent surface.
    parts: list[str] = []
    if name:
        parts.append(f"This is {name}.")
    if service:
        parts.append(f"I need {service}.")
    if body:
        parts.append(body if body.endswith((".", "!", "?")) else body + ".")
    # carry through any extra free-text fields we didn't map, so nothing is lost
    for k, v in data.items():
        if k in _FORM_NAME or k in _FORM_PHONE or k in _FORM_BODY or k in _FORM_SERVICE:
            continue
        if isinstance(v, str) and v.strip():
            parts.append(f"{k.replace('_', ' ')}: {v.strip()}.")
    transcript = " ".join(parts).strip() or (body or service or "Service request.")
    return Payload(caller=phone, transcript=transcript, source=FORM)


# --------------------------------------------------------------------------- #
# Source 3 — an inbound email (raw RFC822 in, same record out)
# --------------------------------------------------------------------------- #

_SIG_CUT = re.compile(
    r"\n\s*(?:--\s*$|sent from my |best,|thanks,|regards,|cheers,)",
    re.IGNORECASE | re.MULTILINE,
)
_PHONE_RE = re.compile(r"(\+?\d[\d\-\.\s\(\)]{7,}\d)")


def _email_body(msg: Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(part.get_content_charset() or "utf-8",
                                          errors="replace")
        return ""
    payload = msg.get_payload(decode=True)
    if payload:
        return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    return str(msg.get_payload())


def from_email(raw_email: str) -> Payload:
    """An inbound service-request email (raw RFC822 text).

    The sender's display name seeds the transcript, the subject + plain-text body
    become the request text, and any phone number in the signature becomes the
    caller id for cross-channel dedupe. The same extractor and the same lane file
    the same record a call would.
    """
    msg = message_from_string(raw_email)
    from_hdr = msg.get("From", "")
    subject = (msg.get("Subject", "") or "").strip()
    name_match = re.match(r'\s*"?([^"<]+?)"?\s*<', from_hdr)
    sender_name = (name_match.group(1).strip() if name_match else "").strip()

    body = _email_body(msg)
    cut = _SIG_CUT.search(body)
    request_text = (body[: cut.start()] if cut else body).strip()

    phone = ""
    pm = _PHONE_RE.search(body)
    if pm:
        phone = pm.group(1).strip()

    parts: list[str] = []
    if sender_name:
        parts.append(f"This is {sender_name}.")
    if subject:
        parts.append(subject if subject.endswith((".", "!", "?")) else subject + ".")
    if request_text:
        parts.append(request_text)
    transcript = " ".join(parts).strip() or subject or "Service request via email."
    return Payload(caller=phone, transcript=transcript, source=EMAIL)


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #

def from_source(source: str, **kwargs) -> Payload:
    """Build a Payload from a named source. Used by the CLI's --source flag."""
    if source == CALL:
        return from_call(kwargs.get("caller"), kwargs.get("transcript", ""))
    if source == FORM:
        return from_form(kwargs["form_json"])
    if source == EMAIL:
        return from_email(kwargs["raw_email"])
    raise ValueError(f"unknown source {source!r} (expected call|form|email)")

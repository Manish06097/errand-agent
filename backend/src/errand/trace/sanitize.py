"""Trace sanitizer. The Tracer runs it once per record, before any sink sees it.

Policies (architecture 16.2):
- review text becomes {review_id, sha256, len}, wherever a `reviews` list appears;
- phone numbers and street addresses are masked, by pattern in free text and by key
  name for fields such as `phone`, `mobile` or `address`;
- API keys (`sk-ant-...`) and header-like fields are redacted;
- thinking blocks keep only the sanitized summary and the signature's sha256.

Strings that hold JSON (tool results inside a rendered request, for instance) are
parsed, sanitized and re-encoded, so a review or phone number nested in a tool result
is caught too. The trace copy is the only thing re-encoded; the model's own history is
never touched by this module.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

PHONE_MASK = "[phone]"
ADDRESS_MASK = "[address]"
KEY_MASK = "[redacted-key]"
SECRET_MASK = "[redacted]"

_API_KEY = re.compile("sk-" + r"ant-[A-Za-z0-9_\-]+")
# Indian mobiles with or without +91, and other +<country> numbers. The look-arounds
# keep it off ids, hashes, epoch milliseconds and ISO timestamps.
# Indian landlines: 0, a 2 to 4 digit STD code (optionally in parentheses), then 6 to 8
# digits with optional spaces or hyphens: "080 4123 4567", "(080) 4123-4567",
# "0471-2345678". A leading-zero mobile ("09845012345") matches too. The extra "."
# guards keep it off decimals like "0.000123456789".
_PHONE = re.compile(
    r"(?<![\w+])(?:(?:\+|00)91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}(?![\w])"
    r"|(?<![\w+])\+\d{1,3}[\s-]?\(?\d{2,5}\)?[\s-]?\d{3,5}[\s-]?\d{3,5}(?![\w])"
    r"|(?<![\w+.])(?:\(0\d{2,4}\)|0\d{2,4})[\s-]?\d(?:[\s-]?\d){5,7}(?!\w|\.\d)"
)
# A house or flat number followed by a street: "12, 4th Cross", "Flat 3B, 100 Feet Road",
# "42 MG Road". An ordinal is required before main/cross/block so "the 3 main options"
# survives, and an area name on its own ("Indiranagar") is not an address.
_ORDINAL_STREET = (
    r"\d{1,3}(?:st|nd|rd|th)\s+(?i:main|cross|block|stage|phase|sector)(?:\s+(?i:road|rd))?"
)
_NAMED_STREET = r"(?:[A-Z0-9][\w.'-]*\s+){1,4}(?i:road|rd|street|lane|avenue|marg)\b\.?"
_STREET = rf"(?:{_ORDINAL_STREET}|{_NAMED_STREET})"
_ADDRESS = re.compile(
    r"\b(?:(?i:flat|house|door|plot|no\.?)\s*)?#?\d{1,5}[A-Za-z]?(?:[/-]\d{1,5}[A-Za-z]?)?,?\s+"
    rf"{_STREET}(?:,?\s+{_STREET})*(?:(?:,?\s+[A-Z][\w.'-]*){{0,3}},?\s+[1-9]\d{{5}}\b)?"
)
_PIN = re.compile(r"(?i)\b(?:pin(?:\s*code)?|pincode)[:\s-]*[1-9]\d{5}\b")
# A pincode right after a place name, with no street before it: "Indiranagar,
# Bengaluru 560038". Six digits after a capitalised word are rare enough elsewhere in a
# trace that masking the odd false positive is the right trade.
_CITY_PIN = re.compile(r"\b[A-Z][a-z]+,?\s+[1-8]\d{5}\b(?!\.\d)")

_SECRET_KEYS = frozenset({"api_key", "anthropic_api_key", "authorization", "x-api-key", "headers"})
_ADDRESS_KEYS = frozenset({"address", "street", "street_address", "address_line", "pincode"})
# Masked by name whatever the value looks like, because no regex knows every number
# format a provider record might carry.
_PHONE_KEYS = frozenset(
    {
        "phone",
        "phone_number",
        "phone_no",
        "mobile",
        "mobile_number",
        "mobile_no",
        "telephone",
        "tel",
        "landline",
        "contact_number",
        "contact_phone",
        "whatsapp",
        "whatsapp_number",
        "alt_phone",
    }
)
_PHONE_SUFFIXES = ("_phone", "_mobile", "_phone_number")
# Identifiers and digests: never rewritten, so masks cannot corrupt them.
_OPAQUE_SUFFIXES = ("_id", "_sha256", "_hash", "fingerprint", "idempotency_key", "_fp")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sanitize_text(text: str) -> str:
    text = _API_KEY.sub(KEY_MASK, text)
    text = _PIN.sub(ADDRESS_MASK, text)
    text = _ADDRESS.sub(ADDRESS_MASK, text)
    text = _CITY_PIN.sub(ADDRESS_MASK, text)
    return _PHONE.sub(PHONE_MASK, text)


def _is_phone_key(name: str) -> bool:
    lowered = name.lower()
    return lowered in _PHONE_KEYS or lowered.endswith(_PHONE_SUFFIXES)


def _review(item: Any) -> Any:
    if not isinstance(item, Mapping) or not isinstance(item.get("text"), str):
        return sanitize(item)
    text = item["text"]
    review_id = item.get("review_id", item.get("id"))
    out = {k: sanitize(v) for k, v in item.items() if k != "text"}
    out["text"] = {"review_id": review_id, "sha256": _sha256(text), "len": len(text)}
    return out


def _thinking(block: Mapping[str, Any]) -> dict[str, Any]:
    signature = block.get("signature")
    return {
        "type": "thinking",
        "thinking": sanitize_text(str(block.get("thinking") or "")),
        "signature_sha256": _sha256(signature) if isinstance(signature, str) else None,
    }


def _maybe_json(text: str) -> Any:
    stripped = text.lstrip()
    if not stripped or stripped[0] not in "{[":
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def sanitize(value: Any, key: str | None = None) -> Any:
    """Return a sanitized deep copy of `value`."""
    if isinstance(value, str):
        if key is not None and key.endswith(_OPAQUE_SUFFIXES):
            return _API_KEY.sub(KEY_MASK, value)
        if key is not None and key.lower() in _ADDRESS_KEYS:
            return ADDRESS_MASK
        if key is not None and _is_phone_key(key):
            return PHONE_MASK
        parsed = _maybe_json(value)
        if parsed is not None:
            return json.dumps(sanitize(parsed), ensure_ascii=False, separators=(",", ":"))
        return sanitize_text(value)
    if isinstance(value, Mapping):
        if value.get("type") == "thinking":
            return _thinking(value)
        if value.get("type") == "redacted_thinking":
            data = value.get("data")
            return {
                "type": "redacted_thinking",
                "data_sha256": _sha256(data) if isinstance(data, str) else None,
            }
        out: dict[str, Any] = {}
        for k, v in value.items():
            name = str(k)
            if name.lower() in _SECRET_KEYS:
                out[name] = SECRET_MASK
            elif name.lower() in _ADDRESS_KEYS and v is not None:
                out[name] = ADDRESS_MASK
            elif _is_phone_key(name) and v is not None and not isinstance(v, bool):
                out[name] = PHONE_MASK
            elif name == "reviews" and isinstance(v, list):
                out[name] = [_review(item) for item in v]
            else:
                out[name] = sanitize(v, name)
        return out
    if isinstance(value, list | tuple):
        return [sanitize(v, key) for v in value]
    return value

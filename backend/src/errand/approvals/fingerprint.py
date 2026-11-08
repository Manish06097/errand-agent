"""The material fingerprint (architecture 8.1).

`fingerprint(m) = "fp1_" + sha256(canonical_json(m))`. The canonical form is the
model's JSON with sorted keys, no whitespace and ASCII escapes, so it does not depend
on dict order, Python's float printing or the platform encoding. The schema version is
a field of the material, so it is part of the hash.

`material(card)` is the only way code gets material from a card: the card carries the
exact material object it was rendered from, so what the user sees and what code
fingerprints share one definition.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import TypeAdapter

from errand.domain.material import BookMaterial, CancelMaterial, Material, RescheduleMaterial
from errand.domain.messages import ActionCard

FINGERPRINT_PREFIX = "fp1_"
_ADAPTER: TypeAdapter[BookMaterial | CancelMaterial | RescheduleMaterial] = TypeAdapter(Material)


def canonical_json(m: BookMaterial | CancelMaterial | RescheduleMaterial) -> str:
    return json.dumps(
        m.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )


def fingerprint(m: BookMaterial | CancelMaterial | RescheduleMaterial) -> str:
    return FINGERPRINT_PREFIX + hashlib.sha256(canonical_json(m).encode("ascii")).hexdigest()


def parse_material(data: Any) -> BookMaterial | CancelMaterial | RescheduleMaterial:
    """Material from its stored JSON (approvals.material_json)."""
    if isinstance(data, str | bytes):
        return _ADAPTER.validate_json(data)
    return _ADAPTER.validate_python(data)


def material(card: ActionCard) -> BookMaterial | CancelMaterial | RescheduleMaterial:
    return card.material

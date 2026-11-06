"""The token-count samples from spikes/claude/s5_tokens.py, rebuilt with the same code.

Kept verbatim (seed, ranges, strings) so the committed fixture can be checked against
the generator; only the API calls are left out.
"""

from __future__ import annotations

import json
import random


def build_samples() -> dict[str, str]:
    rng = random.Random(7)
    english = (
        "Find a plumber for my kitchen tap this week, mornings only, under Rs 1,500. I live in "
        "Indiranagar and the tap has been leaking since Tuesday. Last time the electrician "
        "came late, so please pick someone with good punctuality reviews. Also, move my AC "
        "servicing to Saturday if possible, but not before 10 a.m. I prefer the same "
        "technician as last time. "
    ) * 4
    providers = [
        {
            "id": f"P{i}",
            "name": f"Provider {i}",
            "price": rng.randint(800, 2200),
            "rating": round(rng.uniform(3.5, 5), 1),
            "slots": [
                f"2026-11-{rng.randint(9, 14):02d}T{rng.randint(7, 17):02d}:00" for _ in range(3)
            ],
            "attributes": {
                "verified": True,
                "years": rng.randint(1, 15),
                "languages": ["en", "kn"],
            },
        }
        for i in range(8)
    ]
    memories = "\n".join(
        f"- [explicit] pref_{i}: earliest_time=0{7 + i % 3}:00 area=Indiranagar technician=T{i}"
        for i in range(50)
    )
    # "Mujhe kal subah plumber chahiye, budget <rupee>1500 tak. <Hindi: the tap is dripping> "
    hindi = ("Mujhe kal subah plumber chahiye, budget ₹1500 tak. नल टपक रहा है। ") * 6
    return {
        "english": english,
        "json_compact": json.dumps(providers),
        "json_pretty": json.dumps(providers, indent=2),
        "memory_lines": memories,
        "hindi_mixed": hindi,
    }

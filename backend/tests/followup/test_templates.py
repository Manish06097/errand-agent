from __future__ import annotations

from datetime import datetime

from errand.clock import IST
from errand.followup import templates as t

NOW = datetime(2026, 11, 9, 8, 0, tzinfo=IST)
SLOT = datetime(2026, 11, 13, 9, 0, tzinfo=IST)


def test_when_names_the_day_or_tomorrow() -> None:
    assert t.when(SLOT, IST) == "Fri 13 Nov at 09:00"
    assert t.when(datetime(2026, 11, 10, 9, 0, tzinfo=IST), IST, now=NOW) == "tomorrow at 09:00"
    assert t.when(datetime(2026, 11, 9, 17, 0, tzinfo=IST), IST, now=NOW) == "today at 17:00"
    assert t.when(SLOT, IST, now=NOW) == "Fri 13 Nov at 09:00"


def test_the_signed_texts() -> None:
    assert t.replacement_notice(
        "Ravi Plumbing", "plumber", "Thu 13 Nov at 09:00", "B-12", "09:22"
    ) == (
        "Ravi Plumbing cancelled your plumber visit on Thu 13 Nov at 09:00 (B-12). "
        "Here is a replacement that fits what you asked for, held until 09:22:"
    )
    assert t.origin_line("Ravi Plumbing", "B-12", "Thu 13 Nov 09:00") == (
        "Ravi Plumbing cancelled B-12 (Thu 13 Nov 09:00)."
    )
    assert t.already_have("Arjun Plumbing", "Fri 14 Nov 10:00", "B-31") == (
        "You already have Arjun Plumbing on Fri 14 Nov 10:00 (B-31), "
        "so I haven't looked for another."
    )
    assert t.lapse_line("B-12") == (
        "The window for replacing B-12 has passed, so I've stopped looking. Ask me any time."
    )
    assert t.ask_area() == "Where should I look?"


def test_the_bell_title() -> None:
    assert t.bell_title("plumber", replacement_ready=True) == (
        "Plumber visit cancelled · replacement ready"
    )
    assert t.bell_title("plumber", replacement_ready=False).endswith("nothing fits yet")
    assert t.bell_title("ac_servicing", replacement_ready=None) == "Ac servicing visit cancelled"

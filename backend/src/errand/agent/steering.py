"""Steering without forced tool choice (architecture 10.4).

When the loop ends with prose that points at a selection but proposes nothing ("Let me
book that for you", "I'll reserve S2", "going ahead with Sharma"), code appends one text
block after the last message and calls the model once more. The system blocks and every
earlier message stay byte for byte as they were, so the thinking signatures bound to
that prefix stay valid. One reprompt per run; a second miss ends the run
MODEL_NO_PROGRESS, with the shortlist card and its slot buttons still on screen.

The detector is a closed lexicon, deliberately narrow and in its own module (it is not a
claim: the claim guard decides what may be said about a booking, this decides when to
ask the model to act). A sentence counts only if it says the assistant is about to book
now: a conditional ("once you approve, I'll book it") or a refusal ("I can't book") never
does, because reprompting those would push the model toward a proposal nobody asked for.
"""

from __future__ import annotations

import re
import unicodedata

REPROMPT_TEXT = (
    "No action was proposed. If you intend to book, call propose_booking with a slot_ref."
)

_INTENT = re.compile(
    r"\b(?:"
    r"let me (?:go ahead and )?(?:book|reserve|schedule|lock in|arrange)"
    r"|i(?:'ll| will| shall) (?:go ahead and |now |just )?(?:book|reserve|schedule|lock in|arrange)"
    r"|i(?:'m| am) (?:going to|about to|now) (?:book|reserve|schedule)"
    r"|(?:i(?:'m| am) )?going ahead (?:and book |and reserve |with )"
    r"|(?:i(?:'ll| will) )?go ahead and (?:book|reserve)"
    r"|(?:booking|reserving) (?:that|this|it|s\d+)\b"
    r"|i(?:'ll| will) take care of (?:the |that )?(?:booking|reservation)"
    r")"
)
_VETO = re.compile(
    r"\b(?:once|if|when|whenever|after|unless|until|can't|cannot|can not|won't|will not|"
    r"don't|do not|not|never|unable|tap|approve|approval|confirm)\b"
)
_SENTENCE = re.compile(r"(?<=[.!?\n])\s+")


def _norm(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text).casefold()
    return folded.replace(chr(0x2019), "'")


def points_at_a_selection(text: str) -> bool:
    """Does the text say the assistant is about to book, without proposing it?"""
    for sentence in _SENTENCE.split(_norm(text)):
        if _INTENT.search(sentence) and not _VETO.search(sentence):
            return True
    return False

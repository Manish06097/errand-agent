"""A scripted planner for a user who takes a value back ("not indiranagar actually").

It is the reference policy plus one move a real model makes: when the current message
retracts the area the brief already holds, it clears that field with the retraction as
its evidence, alongside whatever else the message gives. Everything else (asks,
searches, recommendations) is the reference policy's, so the ask guard, not this
policy, decides whether the area may be asked again.
"""

from __future__ import annotations

import re

from langchain_core.messages import AIMessage

from errand.llm.scripted import ScriptedRequest
from evals.policies import reference
from evals.policies.common import read, tool_calls

_AREAS = "|".join(
    re.escape(alias) for aliases in reference.AREA_ALIASES.values() for alias in aliases
)
_RETRACT_AREA = re.compile(rf"\bnot (?:in )?(?:{_AREAS})(?: actually)?\b", re.I)


def policy(request: ScriptedRequest) -> AIMessage:
    view = read(request)
    retraction = _RETRACT_AREA.search(view.current) if not view.loop else None
    if retraction is None or "area" not in view.known:
        return reference.policy(request)
    patch = reference.extract(view.current, view)
    args = reference.update_brief_args(patch)
    args["set"]["area"] = None
    args["clear"] = ["area"]
    args["evidence"] = [
        *(e for e in args["evidence"] if e["field"] != "area"),
        {"field": "area", "quote": retraction.group(0)},
    ]
    return tool_calls(
        request,
        "The user took the area back; clear it and record the rest.",
        ("update_brief", args),
    )

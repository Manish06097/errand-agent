"""Shared helpers for the spike. Loads the API key from the project .env without printing it."""
import json, os, time
from dotenv import dotenv_values

_vals = dotenv_values(os.environ.get("SPIKE_ENV_FILE", os.path.join(os.path.dirname(__file__), "..", "..", ".env")))
os.environ["ANTHROPIC_API_KEY"] = _vals["ANTHROPIC_API_KEY"]  # never printed

OUT = os.path.join(os.path.dirname(__file__), "out")
PRICES = {  # USD per 1M tokens
    "claude-opus-5-5": {"in": 4.0, "out": 20.0},
    "claude-haiku-5-5": {"in": 0.10, "out": 0.50},
}

def dump(name, obj):
    with open(os.path.join(OUT, name), "w") as f:
        json.dump(obj, f, indent=2, default=str)

def msg_summary(m):
    return {
        "type": type(m).__name__,
        "content": m.content,
        "tool_calls": getattr(m, "tool_calls", None),
        "usage_metadata": getattr(m, "usage_metadata", None),
        "response_metadata": getattr(m, "response_metadata", None),
    }

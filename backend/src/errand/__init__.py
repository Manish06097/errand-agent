"""Errand agent backend.

LangGraph reads LANGGRAPH_STRICT_MSGPACK once, when its serde module is first
imported. Setting it here, in the root package, means it is on before any errand
module can import langgraph, whatever order an importing module lists its imports in:
checkpoints then deserialise only the types reachable from `ThreadState` (6.1).
"""

import os

os.environ["LANGGRAPH_STRICT_MSGPACK"] = "true"

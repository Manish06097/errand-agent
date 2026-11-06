"""The LangGraph agent: state, graph wiring and nodes.

Strict msgpack (LANGGRAPH_STRICT_MSGPACK) is switched on in `errand/__init__.py`, which
runs before any errand module imports langgraph; `graph.build_graph` refuses to compile
if it did not take.
"""

"""Scenario selected by argv[1]: nokeys | disabled | unreachable"""
import os, sys, time
sys.path.insert(0, os.path.dirname(__file__))
import netguard
mode = sys.argv[1]
for k in list(os.environ):
    if k.startswith("LANGFUSE_"): del os.environ[k]
if mode in ("disabled", "unreachable"):
    os.environ["LANGFUSE_PUBLIC_KEY"] = "pk-lf-test"; os.environ["LANGFUSE_SECRET_KEY"] = "sk-lf-test"
    os.environ["LANGFUSE_BASE_URL"] = "http://127.0.0.1:9"  # closed port
if mode == "disabled":
    os.environ["LANGFUSE_TRACING_ENABLED"] = "false"

t0 = time.perf_counter()
from langfuse import get_client
from langfuse.langchain import CallbackHandler
from langchain_core.language_models.fake_chat_models import FakeListChatModel
t_import = time.perf_counter() - t0

handler = CallbackHandler()
llm = FakeListChatModel(responses=["hello"])
out = llm.invoke("hi", config={"callbacks": [handler], "metadata": {"langfuse_session_id": "s1"}})
lf = get_client()
with lf.start_as_current_observation(name="manual", as_type="span") as sp:
    sp.update(output={"ok": True})
t1 = time.perf_counter(); lf.flush(); t_flush = time.perf_counter() - t1
t2 = time.perf_counter(); lf.shutdown(); t_shut = time.perf_counter() - t2
print(f"MODE={mode} output={out.content!r} tracing_enabled={lf._tracing_enabled} resources={lf._resources is not None}")
print(f"import={t_import:.2f}s flush={t_flush:.2f}s shutdown={t_shut:.2f}s")
print("NETWORK:", [a for a in netguard.ATTEMPTS])
print("LOGS (non-DEBUG):"); [print("  ", r) for r in netguard.CAP.records if not r.startswith("DEBUG")]

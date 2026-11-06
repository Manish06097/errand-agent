import os, time, logging
for k in list(os.environ):
    if k.startswith("LANGFUSE_"): del os.environ[k]
logging.basicConfig(level=logging.ERROR)
from langfuse import Langfuse
lf = Langfuse(public_key="pk", secret_key="sk", base_url="http://10.255.255.1:3000", timeout=int(os.environ.get("T","5")))
with lf.start_as_current_observation(name="x"): pass
t=time.perf_counter(); lf.flush(); print("flush blocked %.1fs (timeout=%s)" % (time.perf_counter()-t, os.environ.get("T","5")))
t=time.perf_counter(); lf.shutdown(); print("shutdown %.1fs" % (time.perf_counter()-t))

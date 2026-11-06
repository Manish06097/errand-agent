#!/usr/bin/env bash
# Re-run every Langfuse experiment and save what each one prints (stdout and stderr) to
# out/<script>.txt.
#
#   SPIKE_PYTHON=/path/to/venv/bin/python ./rerun.sh
#
# No Langfuse server is involved: the scripts point the SDK at a closed loopback port
# (127.0.0.1:9) or a blackhole address (10.255.255.1) and capture spans in memory.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="${SPIKE_PYTHON:-$HERE/.venv/bin/python}"
OUT="$HERE/out"
mkdir -p "$OUT"
cd "$HERE"
export PYTHONDONTWRITEBYTECODE=1

run() {
  local name="$1"; shift
  local envs=()
  while [[ "$1" == *=* ]]; do envs+=("$1"); shift; done
  local script="$1"; shift
  {
    echo "\$ ${envs[*]:+${envs[*]} }python scripts/$script $*"
    env ${envs[@]+"${envs[@]}"} "$PY" "scripts/$script" "$@" 2>&1
    echo "[exit $?]"
    echo
  } >> "$OUT/$name.txt"
}

rm -f "$OUT"/*.txt
{ echo "re-run: $(date -u +%Y-%m-%dT%H:%M:%SZ)"; "$PY" -c 'import importlib.metadata as m, sys; print("python", sys.version.split()[0]); [print(p, m.version(p)) for p in ("langfuse", "opentelemetry-sdk", "langgraph", "langchain-core")]'; } > "$OUT/versions.txt"

run t_noop t_noop.py nokeys
run t_noop t_noop.py disabled
run t_noop t_noop.py unreachable
run t_capture t_capture.py
run t_replay t_replay.py
run t_sink t_sink.py
run t_blackhole T=2 t_blackhole.py
run t_blackhole T=5 t_blackhole.py
echo "outputs in $OUT"

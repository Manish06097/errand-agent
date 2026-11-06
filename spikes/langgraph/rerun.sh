#!/usr/bin/env bash
# Re-run every LangGraph experiment in a fresh temp directory and save what each one
# prints (stdout and stderr) to out/<script>.txt, plus the ledgers the crash runs write.
#
#   SPIKE_PYTHON=/path/to/venv/bin/python ./rerun.sh
#
# The venv must have requirements.txt installed (the spike's exact package set).
# Nothing here talks to the network; all SQLite files stay in the temp directory.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="${SPIKE_PYTHON:-$HERE/.venv/bin/python}"
SCRIPTS="$HERE/scripts"
OUT="$HERE/out"
WORK="$(mktemp -d)"
mkdir -p "$OUT" "$WORK/cwd"
cd "$WORK/cwd"
export PYTHONDONTWRITEBYTECODE=1

# run <outfile> [VAR=value ...] <script> [args...]
run() {
  local name="$1"; shift
  local envs=()
  while [[ "$1" == *=* ]]; do envs+=("$1"); shift; done
  local script="$1"; shift
  {
    echo "\$ ${envs[*]:+${envs[*]} }python scripts/$script $*"
    env ${envs[@]+"${envs[@]}"} "$PY" "$SCRIPTS/$script" "$@" 2>&1
    echo "[exit $?]"
    echo
  } >> "$OUT/$name.txt"
}

rm -f "$OUT"/*.txt "$OUT"/*.ledger "$OUT"/*.json
{ echo "re-run: $(date -u +%Y-%m-%dT%H:%M:%SZ)"; "$PY" -c 'import importlib.metadata as m, sys; print("python", sys.version.split()[0]); [print(p, m.version(p)) for p in ("langgraph", "langgraph-checkpoint", "langgraph-checkpoint-sqlite", "langchain-core", "aiosqlite")]'; } > "$OUT/versions.txt"

# Interrupt and resume in two separate processes; counters show the node re-running.
run s1_interrupt s1_interrupt.py start s1.sqlite s1_counter.json
run s1_interrupt s1_interrupt.py inspect s1.sqlite s1_counter.json
run s1_interrupt s1_interrupt.py resume s1.sqlite s1_counter.json
run s1_interrupt s1_interrupt.py inspect s1.sqlite s1_counter.json
run s1_interrupt s1_interrupt.py start_v2 s1_v2.sqlite s1_v2_counter.json
cp s1_counter.json "$OUT/s1_counter.json"

run s1b_edge s1b_edge.py

# Hard crash after the side effect, then restart, once per durability mode.
for d in async sync exit; do
  run s3_crash CRASH=1 s3_crash.py run "s3_$d.sqlite" "s3_$d.ledger" "$d"
  run s3_crash s3_crash.py inspect "s3_$d.sqlite" "s3_$d.ledger" "$d"
  run s3_crash s3_crash.py restart "s3_$d.sqlite" "s3_$d.ledger" "$d"
  cp "s3_$d.ledger" "$OUT/s3_$d.ledger"
done

# Write-ahead intent table in the same SQLite file as the checkpoints.
run s3b_writeahead CRASH=1 s3b_writeahead.py run s3b.sqlite s3b_provider.sqlite
run s3b_writeahead s3b_writeahead.py restart s3b.sqlite s3b_provider.sqlite
{
  echo "\$ tables in s3b.sqlite (checkpointer and our table, one file)"
  "$PY" -c 'import sqlite3; print(sorted(r[0] for r in sqlite3.connect("s3b.sqlite").execute("select name from sqlite_master where type=\"table\"")))'
} >> "$OUT/s3b_writeahead.txt"

run s3c_exc s3c_exc.py
run s4_stream s4_stream.py
run s4b_concurrency s4b_concurrency.py
run s5_inspect s5_inspect.py

run s6_serde s6_serde.py write s6.sqlite
run s6_serde s6_serde.py read s6.sqlite
run s6_serde LANGGRAPH_STRICT_MSGPACK=true s6_serde.py read s6.sqlite
run s6_serde s6b_allow.py s6.sqlite

echo "outputs in $OUT; scratch files in $WORK"

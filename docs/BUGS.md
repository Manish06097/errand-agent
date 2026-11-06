# Bugs

Real defects in this codebase that the eval suite, a test or a manual run caught. Each
one is reproduced end to end first: the failing repro is committed on its own (the
commit message says "failing repro"), the fix is a separate commit, and the same repro
is re-run after the fix. Library behaviours found in the spikes are design inputs, not
bugs (architecture section 19).

| id | what broke | why | fix | test | repro SHA | fix SHA |
|---|---|---|---|---|---|---|
| BUG-001 | After `make dev` under a coding agent, an untracked `web/AGENTS.md` appeared; `git add -A` then failed the em-dash guard and would have committed tool-generated agent instructions | Next 16.4's dev server detects coding agents (via `AI_AGENT` and similar variables) and writes a managed `AGENTS.md` at the project root unless `agentRules: false`; its text contains U+2014 | `agentRules: false` in `web/next.config.ts` | `backend/tests/regressions/test_bug_001_dev_server_writes_agent_files.py` | 8d14f54 | 48c2d0d |

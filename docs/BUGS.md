# Bugs

Real defects in this codebase that the eval suite, a test or a manual run caught. Each
one is reproduced end to end first: the failing repro is committed on its own (the
commit message says "failing repro"), the fix is a separate commit, and the same repro
is re-run after the fix. Library behaviours found in the spikes are design inputs, not
bugs (architecture section 19).

| id | what broke | why | fix | test | repro SHA | fix SHA |
|---|---|---|---|---|---|---|
| BUG-001 | After `make dev` under a coding agent, an untracked `web/AGENTS.md` appeared; `git add -A` then failed the em-dash guard and would have committed tool-generated agent instructions | Next 16.4's dev server detects coding agents (via `AI_AGENT` and similar variables) and writes a managed `AGENTS.md` at the project root unless `agentRules: false`; its text contains U+2014 | `agentRules: false` in `web/next.config.ts` | `backend/tests/regressions/test_bug_001_dev_server_writes_agent_files.py` | 8d14f54 | 48c2d0d |
| BUG-M2-1 | Filling the Observability section of `docs/DESIGN_NOTE.md` (65 of 70 words, a valid note) turned `make check` red (`test_total_over_550_fails_even_if_sections_fit` failed); with every section filled, 8 of the 16 tests in `tests/hygiene/test_design_note.py` fail | Those tests built their broken notes by adding words to the real note, so they assumed every section was still empty; any milestone filling its section would break them | The tests use a skeleton of the real note (headings and comments only, prose dropped) | `backend/tests/regressions/test_bug_m2_1_design_note_tests_need_an_empty_note.py` | 8273ef1 | e533650 |

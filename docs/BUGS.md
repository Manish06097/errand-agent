# Bugs

Real defects in this codebase that the eval suite, a test or a manual run caught. Each
one is reproduced end to end first: the failing repro is committed on its own (the
commit message says "failing repro"), the fix is a separate commit, and the same repro
is re-run after the fix. Library behaviours found in the spikes are design inputs, not
bugs (architecture section 19).

| id | what broke | why | fix | test | repro SHA | fix SHA |
|---|---|---|---|---|---|---|

# Three bugs

The full log is [BUGS.md](BUGS.md), 30 bugs found by tests, reviews and manual runs. Each one was reproduced end to end first, the failing repro was committed on its own, the fix came as a separate commit, and the same repro was re-run afterwards. A test (`backend/tests/hygiene/test_bug_commit_order.py`) checks with git that every repro commit below is an ancestor of its fix commit.

I picked these three because each one is a different way the system could have lied to the user or to me, and each was caught by a check I had written, not by luck:

- **BUG-M1-3** is the double booking, the failure the whole idempotency design exists to prevent, and it sat in the world I test against.
- **BUG-M3-1** is the safety net itself failing: the eval gate went green on zero scenarios.
- **BUG-M4-2** is the agent saying "booked" with nothing behind it, and it took five review rounds to close, which says more about the problem than the first fix did.

## BUG-M1-3: a used hold booked a second time

**What broke.** I booked hold H with key k1, cancelled that booking, then called `book(H, k2)`. The mock world answered "confirmed" and created a second booking on H. It happened after a provider cancel and after a reschedule too, and `get_booking_status(hold_id=H)` then named the cancelled booking while H had a live one, so the world contradicted itself.

**Why.** `_book` only asked whether another key's live booking held the hold's slot. Once the slot was free again nothing remembered that the hold had been used.

**Fix.** A consumed hold books only for the key that consumed it. Another key gets `slot_taken` while the booking is live and `invalid_args` ("hold already used") after it is cancelled. The consuming key still falls through, so same-key behaviour is unchanged.

**Repro commit** `87f1329`. **Fix commit** `18a9719`. **Regression test** `backend/tests/contract/test_bug_m1_3_consumed_hold_rebook.py`, which covers cancel, provider cancel and reschedule, dedupe on and off, over both transports.

## BUG-M3-1: `make evals` passed with no scenarios

**What broke.** I renamed the scenario files to `.yml` (and separately deleted them). `make evals` printed "1 skipped" and exited 0, and with `--write-report` it silently kept the old table. A misnamed file next to real scenarios was never run either. A green eval gate meant nothing.

**Why.** The test file parametrizes over `scenarios/*.yaml`. Pytest turns an empty parameter list into one skipped test (exit 0), not "no tests collected" (exit 5), so the wrapper's exit-5 check never fired. The earlier tests that guarded this had been deleted instead of adapted.

**Fix.** An empty catalogue, or any entry in `scenarios/` that is not a `*.yaml` file, is a collection error (exit 2). Pytest runs with `empty_parameter_set_mark = fail_at_collect`, and the report plugin refuses a table with no rows or a missing row.

**Repro commit** `cd29408`. **Fix commit** `8ced0c1`. **Regression test** `backend/tests/regressions/test_bug_m3_1_evals_pass_with_no_scenarios.py`.

## BUG-M4-2: false "booked" claims got through the claim guard

**What broke.** Lines like "Your plumber is booked for Saturday, no worries." and "All set, Ravi will come at 10, not to worry." reached the transcript with no booking behind them, and my invariant INV-04 could not see it. The same word list also stripped harmless text such as "sorted by price".

**Why.** The guard exempted any sentence that held a negation-like word anywhere ("no", "nothing", "not"), and INV-04 had its own copy of the same idea, so the check shared the blind spot of the thing it checks.

**Fix.** A negation exempts a claim only where it scopes over the claim verb, in the same clause. Idioms such as "no worries" are stripped first. INV-04 got its own positional rule with its own word list and shares no code with the guard. Review found four more holes (bridge words, conditionals, modals, provider names); each got its own repro and fix pair, listed in the log row.

**Repro commit** (round 1) `6fdc8e0`. **Fix commits** `5bcfda3` and `67418a6`. Round 5 is `b16e6db` then `d4bc3ed`. **Regression tests** `backend/tests/regressions/test_bug_m4_2_negation_word_exempts_a_false_claim.py`, `backend/tests/agent/test_claim_guard.py` and `backend/tests/evals/test_invariants.py`.

## The bug found in the last milestone

BUG-N4-1 (a user message stamped after the card it produced, because the API's clock was stale after the world's clock moved behind its back) is in the log with its repro `1ce66f3` and fix `30fa803`. It is not in the three above because it only shows on the dev setup, not in a real deployment where nothing resets the provider's clock.

# Timeout after success: the chat and the trace, side by side

This is a live conversation (real Opus 5.5 planner, real Haiku 5.5 reader, the mock world over HTTP, the system-offset world clock `make dev` uses). The booking `book` call is forced to time out after the world committed the booking. That is done through the dev drawer's "Next book times out after success" control (`POST /api/dev/next-book {"outcome": "timeout_booked"}`), set once before the click. The timeout is forced, not waited for: the world draws a timeout after success on 7.5% of books, and a demo cannot wait for that.

The file next to this one, [timeout_after_success.jsonl](timeout_after_success.jsonl), is the whole conversation from the first message to `BOOKED_VERIFIED`, 71 records over 3 runs, written by `errand-trace export` and read back through the masked reader (so a forgotten value would be masked in it). Phone numbers, addresses and keys were masked, and review text hashed, once, when each record was written. The first record is `export.header` and the last is `conversation.totals`.

```
errand-trace export --thread c_01a1bf6918127b07945613980c8a2ab7 --out docs/traces/timeout_after_success.jsonl
errand-trace excerpt docs/traces/timeout_after_success.jsonl --kind timeout
```

Why three runs: a run is one graph invocation, and the approval click is its own run. Run 1 takes the opener, run 2 takes the user's choice and ends parked on the card, run 3 is the click.

## Side by side

| What the user sees | What the trace says (`seq` is within the run) |
|---|---|
| **User:** "Book a plumber in Indiranagar for my kitchen tap this week, mornings only, under ₹1,500." | Run 1 `r_01a1bf69186774d6..`, `run.start` trigger `user_message`. `context.built` shows every section under its cap (rules 1592 of 1600, tools 3069 of 3800, state 137 of 1100). |
| (a moment of nothing, about 11 s) | Run 1 `model.call` seq 6: Opus, effort medium, 238 uncached + 2733 cached input, 325 output, $0.0080, 2.9 s, `stop_reason: tool_use`. It proposes the brief patch. Seq 9: a second call, 27 output tokens, $0.0041, asks for `search_providers`. |
| | seq 10 `tool.call search_providers` (caller `planner`, 9 ms), seq 11 to 14 `get_provider` (caller `ranker`, code, not the model). |
| | seq 15 to 18: four `reader.call` records, Haiku 5.5, one per shortlisted provider. Three were cache hits (zero cost); one ran for $0.00027 and 3.8 s. The planner never sees review text, only the reader's labels. |
| | seq 21 `model.call`: 1774 uncached + 2733 cached in, 221 out (103 of them reasoning), $0.0121, `stop_reason: end_turn`. |
| **Agent:** a shortlist card, six plumbers, "Nothing booked for this request yet." Manjunath Plumbing Works first (₹500, 4.8, verified, Tue 10 Nov 08:00-09:00). | Run 1 `run.end` outcome `PRESENTED_OPTIONS`, 3 model calls, 1 reader call, $0.0244. |
| **User:** "Take the first one, the earliest slot please." | Run 2 `r_01a1bf696a2d7064..`, `model.call` seq 6: 1107 uncached + 2733 cached in, 189 out, $0.0088, 4.3 s, `tool_use`. The planner picks a provider and slot; code does the rest. |
| | seq 7 and 10 `list_bookings` (caller `proposer`), seq 12 `hold_slot` P13 for Tue 10 Nov 08:00 (H-6649, quoted ₹500, held 10 minutes). Then `approval.requested` with fingerprint `fp1_350ce38c3c58..`. |
| **Agent:** an action card, "Book Manjunath Plumbing Works", Tue 10 Nov 08:00-09:00, ₹500 (same as the search price), button "Book for ₹500". "Not booked yet: waiting for your approval." | Run 2 `run.end` outcome `AWAITING_APPROVAL`, 1 model call, $0.0088. The model proposed; nothing was booked. |
| **User:** clicks "Book for ₹500". | Run 3 `r_01a1bf69a169745f..`, trigger `approval_decision_resume`. `approval.granted` via `button` with the same fingerprint, then `approval.consumed` (one conditional UPDATE), then `intent.written` (attempt 1, key `bo_2a16c1ecf37a..`, hold H-6649) before anything is sent. |
| (the card says it is working) | seq 13 `tool.call book` with that key: `error: ToolTimeout`, 8001 ms. The world had already committed the booking. `intent.unknown`, then `state.transition executing -> verifying`, reason `book_unknown`. No second `book` is sent. |
| | seq 18 `tool.call get_booking_status` with the same key (3 ms): `confirmed`, `B-8863`, the same slot. seq 19 `list_bookings` re-reads the world (caller `verifier`). `status.check` decision `report_success`. |
| | `intent.resolved` state `succeeded`, `book_calls: 1`, then `state.transition verifying -> completed`, reason `status_confirmed`. |
| **Agent:** "Booked Manjunath Plumbing Works for Tue 10 Nov, 08:00-09:00 at ₹500 (B-8863). Confirmed with the provider." The outcome card carries the verified badge. | Run 3 `run.end` outcome `BOOKED_VERIFIED`, 0 model calls (the approval run makes none by design), 4 tool calls, 8.5 s. |

After the run, `GET /api/dev/bookings` listed the two seeded bookings and exactly one new one, `B-8863`: the timeout cost no second booking.

## The totals record

`conversation.totals`, summed from the records in the file with Decimal: 3 runs, 4 model calls, 1 reader call, 12 tool calls, 16,252 tokens in, 1,013 tokens out, $0.0331652. The three `run.end` totals add up to the same numbers (INV-14 holds per run; the export check does it across runs).

## The excerpt

What `errand-trace excerpt --kind timeout` prints, and what the design note carries (10 lines):

```
{"seq":6,"type":"model.call","run_id":"r_01a1bf696a2d..","model":"claude-opus-5-5","effort":"medium","usage":{"input_uncached":1107,"cache_read":2733,"cache_write_5m":0,"cache_write_1h":0,"output":189,"reasoning":109},"cost_usd":0.0088,"latency_ms":4295.3683,"stop_reason":"tool_use"}
...
{"seq":3,"type":"approval.granted","approval_id":"ap_r_01a1bf696..","fingerprint":"fp1_350ce38c3c..","via":"button"}
{"seq":8,"type":"approval.consumed","approval_id":"ap_r_01a1bf696..","intent_id":"in_ap_r_01a1bf.."}
{"seq":10,"type":"intent.written","intent_id":"in_ap_r_01a1bf..","attempt":1,"idempotency_key":"bo_2a16c1ecf37..","hold_id":"H-6649"}
{"seq":13,"type":"tool.call","tool":"book","args":{"hold_id":"H-6649","idempotency_key":"bo_2a16c1ecf37.."},"error":"ToolTimeout","latency_ms":8000.857}
{"seq":17,"type":"state.transition","from":"executing","to":"verifying","reason":"book_unknown"}
{"seq":18,"type":"tool.call","tool":"get_booking_status","args":{"idempotency_key":"bo_2a16c1ecf37.."},"result":{"status":"confirmed","booking_id":"B-8863"}}
{"seq":21,"type":"intent.resolved","intent_id":"in_ap_r_01a1bf..","state":"succeeded","booking_id":"B-8863","book_calls":1}
{"seq":27,"type":"run.end","outcome":"BOOKED_VERIFIED","totals":{"model_calls":0,"tool_calls":4,"cost_usd":0.0}}
```

Read it as: the model proposed (line 1), a person approved (line 3), the intent was written before the call (line 5), the call timed out (line 6), the answer was not guessed but looked up by the same key (line 8), and one booking exists (line 9).

## What this trace is not

- It is one conversation on one build. The timeout is forced; the 7.5% draw is not shown.
- The first message on this model produced a shortlist, not a card, so there is a second user turn. The scripted planner (and the S22 eval row) goes straight to the card. Both are valid paths; the invariants hold on either.
- The git SHA in the header is the checkout that ran the export. `run.start` records do not carry one, and the header says so (`git_sha_source: exporting_checkout`).
- The reader's labels for three of the four providers were cache hits from earlier conversations on the same data dir, which is why their cost is zero.

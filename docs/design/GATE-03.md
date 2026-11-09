# Gate 3: proactive follow-up policy

Status: signed off on 2026-11-09, all four questions taken as recommended. Nothing of N3 is built yet. Part B starts from what migration 0001 and M1 already have: `followup_jobs`, `notifications`, `known_bookings`, `conversations.kind = 'followup'`, the `one_replacement` index, and the world's `provider_cancelled` events with `fire_now` for the dev drawer.
Detail lives in `architecture.md` section 14 and ADR 0014 (brackets are section numbers). A bracket means the design already decided it, **new** marks a call it was silent on, **amends** a change to a written section.

## Decisions

### What the watcher watches

1. **A lifespan task polls `list_bookings` every 15 s; evals call `watcher.tick()`** [14.1]. **New:** it polls every user with a conversation, not only users with rows in `known_bookings`, and mirrors every upcoming booking the provider returns, including seeded ones and ones made elsewhere (question 1). Those land in `known_bookings` with `source_intent_id` NULL and no origin brief. They are provider records, so the claim guard may cite them. A quiet tick writes no trace; a tick that sees a change or fails is a `watch` run. A failed `list_bookings` skips that user until the next tick and shows as watcher lag in `/api/health` [16.5]. You hear nothing about it.
2. **Only a move to `cancelled_by_provider` starts a follow-up** [14.1, 9.4] - the unique `followup_jobs` row and, in the same transaction, `booking_no_longer_active` on every live cancel or reschedule card for that booking [8.5]. **New:** a booking first seen already `cancelled_by_provider` with a future slot counts too (it happened while the agent was down). Every other change (`completed`, cancelled by you elsewhere, a moved slot) is mirrored and nothing more.

### Whether and how to notify

3. **A pure policy function decides, first match wins** [14.3]:

   | Situation | Action |
   |---|---|
   | slot already started, or a replacement for it already succeeded | `ignored`, no notification (S41) |
   | **new:** another confirmed booking of the same service is already inside the replacement window | notify, no search: "You already have Arjun Plumbing on Fri 14 Nov 10:00 (B-31), so I haven't looked for another." |
   | a cancel card for it was pending or granted | notify "Ravi cancelled on his side, so there is nothing for you to cancel", no offer (question 4) |
   | a reschedule card for it was pending or granted | notify, search in the window you were moving to |
   | slot within 24 h | urgent |
   | otherwise | normal |

4. **Where** [14.2] - its own conversation (`kind = followup`, `origin_booking_id`), one line with a link in the origin conversation, one `notifications` row for the bell over `GET /api/users/me/events` [17.3]. Urgent adds a toast and a banner. **Amends N3 part B in `milestones.md`**, which says "in the booking's conversation": a Rebook card there would collide through `one_live_approval` with whatever card that conversation has open.
5. **What** - code templates from structured facts, no model [14.7]. Follow-up conversation: "Ravi Plumbing cancelled your plumber visit on Thu 13 Nov at 09:00 (B-12). Here is a replacement that fits what you asked for, held until 09:22:" then the Rebook card. Urgent says "tomorrow at 09:00". Origin: "Ravi Plumbing cancelled B-12 (Thu 13 Nov 09:00). [Open follow-up]". Bell: "Plumber visit cancelled · replacement ready" or "· nothing fits yet".
6. **How often: one notification per cancel, never a reminder** (**new**). `notifications.event_id` is the job id. A lapsed hold or an expired card updates in place over SSE, never as a new notification.
7. **Quiet rules: `contact_window`** (**new**; gate 2 left its meaning to this gate). With one saved, a normal notice detected outside it is deferred to the window's next opening, and an urgent one is delivered at once with the badge only, no toast or banner. Deferral ends early the moment you are active (any request, or the SSE stream connecting). Search and hold run at delivery, never at detection, so a deferred notice never shows a quote that lapsed overnight. Without a `contact_window` nothing is deferred (question 2). Migration: `followup_jobs` gains state `deferred` and `deliver_at`; the reconciler resumes `detected` jobs, and `deferred` ones only once `deliver_at` has passed [9.7].

### Replacement search

8. **The brief is the original errand's, re-resolved** [14.4] - code clones `origin_brief_json` into a REBOOK errand with `replaces_booking_id` [7.2], then applies gate 2 decision 10: memory-sourced fields are re-read from current memory (forgotten ones cleared, corrected ones refilled from the successor), and fields you stated yourself are kept. **New:** a booking with no origin brief gets one built from the booking (service, day) plus current memory. With no area known there is no search; the notice asks "Where should I look?" and your reply is a normal plan turn.
9. **Window** - the original day ±2 days, starting no earlier than now + 2 h [14.4]; urgent, today and tomorrow only [14.3]. Time-of-day and weekday constraints come from the re-resolved brief and current `earliest_time`, `latest_time` and `avoid_days`.
10. **Same price ceiling** (**new** in this form; 14.4 says "memory or the original price") - the first that exists: current `budget_ceiling` for the service, the original brief's numeric budget, the price you approved for the cancelled booking (its consumed approval's `quoted_price_inr`). A budget of "none" falls through to the approved price (question 3). It is a hard filter on the held quote [8.6].
11. **Filters and ranking are the normal ones** [8.6, 10.2, 10.6] - `avoid_provider`, eligibility, duplicate guard, budget, code ranking. The cancelling provider sorts after every other fit and is not excluded [14.4]. **New:** no reader call in a follow-up run; `review_signal` uses `reader_cache` hits only and unread providers score 0, so "no model call until you reply" holds literally.
12. **How many** (**new**) - the top candidate is held and becomes the Rebook card; the next two show as slot buttons without holds, and a tap is `select_slot`, which holds it and supersedes the card. One `search_providers`, up to three `get_provider`, one `hold_slot`.
13. **Nothing fits** [14.3, S40] - say exactly what was searched, hold nothing, offer three buttons: "More days" (the next 7 days), "Up to ₹N" (N is the cheapest quote that fits everything else, shown only if one exists), "Any time of day" (drops time and weekday constraints). Each click widens one thing for this errand only, as a `ui_action` brief patch and never a memory write, then reruns the same code search.

### The Rebook card and its approval

14. **An ActionCard labelled "Rebook"** [14.5, 17.2] - header "Replaces B-12, cancelled by Ravi Plumbing"; the usual book lines (provider, slot, held price with its delta against the search price, any `same_service_nearby`); **new**, the difference against the cancelled booking's price when we know it ("₹50 more than B-12"). Buttons "Rebook for ₹1,300" and "Not this one"; the notice also has "I'll sort it myself". **New:** `replaces_booking_id` joins the book material only when set, so plain book fingerprints do not change, and the typed grammar accepts "rebook it" as well as "book it" [8.3].
15. **Fresh approval, never an automatic rebook** [14.5, INV-01] - the only way to `book` is a grant on this card in the follow-up conversation, by button or typed confirmation. The original approval was consumed long ago. `one_replacement` [9.2] means a replacement asked for in another chat cannot also be booked. Verified: `BOOKED_VERIFIED`, one line in the origin conversation, job `done` (`replaced`). "Not this one" is `DECLINED` with the options kept. "I'll sort it myself" ends the job `done` (`dismissed`) and invalidates the card. A reply hands over to the planner with the brief intact [14.7].

### If you do nothing

16. **Nothing happens on its own** (**new**) - the hold lapses after 10 min and the card shows "Hold expired · Refresh hold" [17.2]; at 30 min the approval expires [8.5] and the card offers "Find options again", which reruns the code search with a fresh hold. No reminder, no second notification, no widening, no re-search, never a booking. The bell entry stays unread. When the replacement window's last day passes, the watcher ends the job `done` (`lapsed`), invalidates any live Rebook card with the new reason `followup_expired`, and posts one line in the follow-up conversation with no notification: "The window for replacing B-12 has passed, so I've stopped looking. Ask me any time."

### Proof

17. S39 (detected with no message, one notification, replacement under current preferences including one changed since booking, fresh grant, old approval unusable), S40 (no hold, one click widens one field, no memory write), S41 (ignored). **New:** S39b (`contact_window` defers until the window opens and ends early on activity, one notification either way), S39c (seeded A2 cancelled, brief from booking and memory; only with question 1 yes), S41b (same-service booking already in the window: notice, no search, no hold), and with question 4 yes an S39 leg with a pending cancel card. **INV-17 amended:** every job ends `notified`, `ignored` or `done`, or is `deferred` with `deliver_at` after the scenario clock; no `model.call` in a follow-up conversation before its first user message; no `book` there without a grant made after the job was detected. Plus N3's Playwright check.

## Questions for you

1. **Watch every upcoming booking the provider lists, or only ones I booked?** *My recommendation: every one (decision 1).* A cancel hurts the same whoever made the booking, and the default world can cancel the seeded A2; the cost is a second brief path (decision 8) that may have to ask for the area.
2. **Quiet hours when you have saved no `contact_window`?** *My recommendation: none, deliver at once.* There is no push channel, so a night notice wakes nobody, and deferring would delay the search while slots go; a default 21:00 to 08:00 would be friendlier if push is ever added.
3. **When the original errand said "no budget", cap the replacement at the price you approved?** *My recommendation: yes (decision 10).* An offer you did not ask for should not cost more than what you agreed to, and "Up to ₹N" is one click; the cost is that a better provider just above that price waits for your click.
4. **Bring back the cancel-card row the scope trim cut?** *My recommendation: yes, one branch in the policy function and one leg in S39.* Without it, someone about to cancel gets offered a rebook of the booking they wanted gone; leaving it out saves a few lines and one scenario leg.

## Decisions taken

1. Every upcoming booking the provider lists is watched, not only ones the agent booked (decision 1), with the second brief path of decision 8.
2. No quiet hours when no `contact_window` is saved: a follow-up is delivered at once.
3. When the original errand had no budget, the replacement is capped at the price I approved (decision 10), with "Up to ₹N" one click away.
4. The cancel-card row comes back: no rebook offer for a booking I am in the middle of cancelling, as one branch in the policy function and one leg in S39.

On sign-off I update `architecture.md` 4.1, 8.1, 8.3, 8.5, 14, 18.5 and 18.6, ADR 0014, and N3 in `milestones.md`.

Signed off: 2026-11-09

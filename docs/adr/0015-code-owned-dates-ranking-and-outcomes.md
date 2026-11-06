# ADR 0015: Code resolves dates, ranks candidates and writes outcome messages

## Context

Three places where a model error would be quiet and costly: date arithmetic ("this week", "Saturday"), the order in which providers are presented, and the wording of consequential outcomes ("booked!").

## Decision

- The model extracts a `TimeWindowExpr`; `timewin.resolve` computes the window from the injected clock and shows it on a chip.
- The model refers to slots only by handles (`S1..Sn`) that code resolves. The planner's `search_providers` takes no arguments; code derives service, area and `date_range` from the resolved brief.
- Code applies hard filters with reasons and computes a transparent score; the model explains and may recommend any eligible candidate.
- Outcome cards and their sentences are code templates from verified records. Whenever the errand is not completed, a code status line sits under any model text, so a paraphrased claim is never the only signal. `claim_guard` matches claim sentences to records by verb and object (this run's verified outcomes and `known_bookings`), not by keyword alone.

## Alternatives considered

- **Model writes ISO dates and slot strings**: quiet off-by-one-day errors become booking errors.
- **Model ranks**: unstable across runs and influenced by whatever text it saw.
- **Model writes the success message**: can hallucinate success.

## Consequences

- `timewin` needs good tests for Indian date phrasing and week boundaries.
- Replies are a little less free-form around outcomes; that is the point.

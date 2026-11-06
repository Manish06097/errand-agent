# ADR 0005: Quarantined reader for untrusted review text

## Context

`get_provider` returns free-text reviews, at least one of which is a prompt-injection attempt. Injection must not alter intent, permissions or approval requirements. The brief allows more agents if they solve a real problem.

## Decision

- Raw `reviews[].text` (and `about`) go only to a reader: Claude Haiku 5.5, no tools, `with_structured_output(method="json_schema")`, adaptive thinking with low effort set explicitly, tagged `nostream`, system prompt stating the input is data.
- Output per review: `review_id`, `sentiment` (enum), `themes` (up to 3 from a fixed enum of 11), `injection_suspected` (bool). No free text at all.
- Code checks `review_id`s against the input, aggregates into a `ReviewDigest`, and caches by `(provider_id, sha256(reviews))`.
- The planner sees a `ProviderView` without reviews, plus the digest.
- `injection_suspected` drives a UI badge, a trace field and exclusion from ranking: `review_signal` uses only the star ratings of unflagged reviews, so an injecting review can lower its provider's rank but never raise it.
- The real guarantee is capability-level: the planner can only propose; brief and memory changes need user-message evidence; approval needs a user click.

## Alternatives considered

- **Raw reviews in the planner context with a "treat as data" instruction**: relies on the model resisting every injection.
- **Regex injection filter**: bypassable (zero-width characters, Hinglish, paraphrase).
- **Reader with a free-text summary**: worked in the spike without echoing the injection, but it is a channel for injected text, so I drop it.
- **No second model, drop reviews entirely**: loses useful signal for comparing providers.

## Consequences

- About $0.0002 per reader call (measured in the spike, `spikes/claude/out/s6_haiku.json`), roughly 1/100 of a planner call, and cached.
- Review nuance is reduced to themes and sentiment; I accept that.
- INV-10 checks that review canaries never appear in planner requests.

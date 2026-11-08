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
- Code enriches the shortlist before ranking (M10): `search_providers` carries no reviews, so without it only providers the planner happened to open would get a digest and the S14 rank comparison would prove nothing. After hard filters and a provisional score without `review_signal`, code opens the top K candidates with `get_provider` as a dedicated `RANKER` caller (allowlist: `get_provider` only), K being what is left of the run's 4 reader calls, the reader labels them in one concurrent batch, and only then come the final score, the order, the slot handles and the search view (architecture 10.6). Up to 4 extra `get_provider` calls per search count against the run's 16 world calls.
- `review_signal` is the mean of `(rating - 1) / 4` over reviews positively labelled clean, and 0 if any review is flagged, the provider was never read, or the read failed. A read where any review that was sent comes back unlabelled fails closed as a whole (`incomplete_labels`): an injection that talks the reader into skipping it gains nothing over being flagged. A refused prompt is marked (`reader_refusals`, same key as the cache, never a source of labels) and never sent again; timeouts and transport errors may be retried in a later run. A reader outage costs the review signal, never the shortlist.
- The reader is never the security control. Its flags are as good as its model: the offline suite uses a keyword double, and the evidence for Haiku itself is a live smoke over the catalogue (journal, M10), which is not part of the gate.

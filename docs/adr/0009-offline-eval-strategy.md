# ADR 0009: Offline eval strategy

## Context

At least 20 automated scenarios with simulated users, asserting outcomes and invariants rather than wording, offline against a scripted or recorded model, with a generated eval table. My standard is end-to-end tests over unit tests for product behaviour.

## Decision

- Scenarios are YAML (world config, fixtures, model mode, simulated user, steps, expectations) driven through the real FastAPI app over `httpx` ASGI with SSE parsing. Restart scenarios run the agent as a subprocess with named fault points.
- Model modes: `scripted` (CI default) with a competent reference policy and hostile policies (direct book, false claim, loop forever, re-ask, injection follower, garbage tools, no evidence); `replay` of recorded cassettes keyed by canonical request hash; `record`; `live`.
- Deterministic simulated users answer structurally and fail the scenario if re-asked a known field.
- Eighteen global invariants over trace, agent DB and world `op_log` run after every scenario, plus scenario-specific expectations. A 200-seed sweep runs the reference policy over the default world distribution.
- A pytest plugin writes `docs/EVAL_TABLE.md` and `docs/eval_results.json` from observed facts, with `model_mode` and `world` (default or named test-only faults) columns. Socket guard and Langfuse-off fixture enforce offline runs.
- The replay subset covers at least one scenario per brief category with real-model cassettes, so the table does not rest only on a planner I wrote.

## Alternatives considered

- **Wording assertions or LLM-as-judge in the gate**: flaky, costly, and prove nothing about the invariants.
- **Unit tests per node**: fast but miss the interactions where these bugs live (routing, locks, recovery).
- **Only recorded cassettes**: real-model coverage, but brittle to any prompt change and unable to express adversarial models.

## Consequences

- The scripted reference policy must be maintained alongside planner tool changes.
- Cassettes are re-recorded deliberately (`make record`), at about $0.05 per scenario.
- The hostile policies are the main evidence that enforcement lives in code.

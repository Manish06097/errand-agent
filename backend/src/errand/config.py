"""Settings for the agent process, read from the environment.

Each group has its own env prefix, the same pattern as `api/app.py`'s `ServeSettings`.
Later milestones extend this module rather than adding settings elsewhere.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ModelMode = Literal["scripted", "replay", "record", "live"]
Effort = Literal["low", "medium", "high", "xhigh", "max"]

# src/errand/config.py -> repo root. Only used to find the gitignored .env.
REPO_ROOT = Path(__file__).resolve().parents[3]


class ModelSettings(BaseSettings):
    """How the planner model is built and which transport carries its requests."""

    model_config = SettingsConfigDict(env_prefix="ERRAND_", extra="ignore", protected_namespaces=())

    # ERRAND_MODEL_MODE. scripted is the offline default (architecture 18.2).
    model_mode: ModelMode = "scripted"
    planner_model: str = "claude-opus-5-5"
    planner_effort: Effort = "medium"
    planner_max_tokens: int = 16_000
    # The SDK retries a failed request (connection errors, 408, 409, 429, 5xx) inside one
    # model.call, so latency and the planner timeout stretch up to (1 + retries) times.
    # Explicit so nothing hides behind the SDK default; M13 sets it against the deadline.
    planner_max_retries: int = Field(default=2, ge=0)
    # Live and record runs opt in to strict prefix binding, so a history edit fails
    # loudly instead of being patched over by drop_block (architecture 10.1).
    thinking_binding_beta: str = "thinking-binding-controls-2026-08-01"
    # Required for replay and record modes.
    cassette_path: Path | None = None


class LimitSettings(BaseSettings):
    """The limits from architecture 15.1 that are enforced in the model gateway so far."""

    model_config = SettingsConfigDict(env_prefix="ERRAND_LIMIT_", extra="ignore")

    context_budget_tokens: int = 12_000
    planner_timeout_s: float = 60.0
    # The run caps from 15.1 that M3 enforces (M13 adds cost and the live deadline).
    model_calls_per_run: int = 8
    loop_iterations: int = 6
    tool_calls_per_run: int = 16
    run_deadline_s: float = 60.0
    run_backstop_s: float = 90.0
    # Per-run cost cap (15.1). M10 applies it to the quarantined reader only: a reader
    # call is skipped once the run has spent this much. M13 applies it to the planner.
    run_cost_cap_usd: float = 0.40


class BookingSettings(BaseSettings):
    """How the agent recovers from an ambiguous `book` (architecture 9.4 steps 6 and 7)."""

    model_config = SettingsConfigDict(env_prefix="ERRAND_BOOKING_", extra="ignore")

    # A same-key retry per approval after `not_found` on a live hold. 0 gives the pure
    # wait rule (the hold-lapse fallback), and the tests build both.
    max_same_key_retries: int = Field(default=1, ge=0)
    # Declared, never guessed: whether the provider treats a repeated key as the same
    # request. With false there is never a second `book` on a hold. The eval harness
    # derives it from the scenario's `world.enforce_idempotency`.
    provider_dedupes_keys: bool = True


class ReaderSettings(BaseSettings):
    """The quarantined review reader (architecture 10.1, 11, 15.1)."""

    model_config = SettingsConfigDict(env_prefix="ERRAND_READER_", extra="ignore")

    model: str = "claude-haiku-5-5"
    effort: Effort = "low"
    # Adaptive thinking counts toward max_tokens, so a max_tokens stop is real and fails
    # closed like any other bad answer.
    max_tokens: int = 1_500
    timeout_s: float = Field(default=20.0, gt=0)
    # Explicit, like the planner's. 0: a reader failure only costs the digest, and every
    # retry would stretch the worst case past the 20 s the deadline rule plans for.
    max_retries: int = Field(default=0, ge=0)
    calls_per_run: int = Field(default=4, ge=0)
    # Newest reviews per provider the reader is given.
    max_reviews: int = Field(default=10, gt=0)


class PathSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ERRAND_", extra="ignore")

    data_dir: Path = Path("data")

    @property
    def agent_db(self) -> Path:
        return self.data_dir / "agent.sqlite"

    @property
    def traces_dir(self) -> Path:
        return self.data_dir / "traces"


class TraceSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ERRAND_", extra="ignore")

    # ERRAND_LANGFUSE=off forces the Langfuse sink off even when keys are present.
    langfuse: str = "auto"


class LangfuseKeys(BaseSettings):
    """Everything the Langfuse sink is built from. The sink passes each value to the
    SDK explicitly, so the SDK never reads LANGFUSE_* itself."""

    model_config = SettingsConfigDict(env_prefix="LANGFUSE_", extra="ignore")

    public_key: SecretStr | None = None
    secret_key: SecretStr | None = None
    # The self-hosted stack from `make langfuse-up` (infra/langfuse) serves web on 3100.
    host: str = "http://localhost:3100"
    # Seconds per export request. Also the bound on how long shutdown may wait for the
    # last export when the server is down (spike F8).
    timeout: int = Field(default=2, ge=1)


class EnvFileSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ERRAND_", extra="ignore")

    # The non-live test fixture points this at /dev/null so no test can read the key.
    env_file: Path = REPO_ROOT / ".env"


class AnthropicCredentials(BaseSettings):
    """The API key, read only when a live or record transport is built."""

    model_config = SettingsConfigDict(extra="ignore")

    anthropic_api_key: SecretStr | None = None


def load_anthropic_credentials() -> AnthropicCredentials:
    env_file = EnvFileSettings().env_file
    # The process environment wins over the file, as pydantic-settings does by default.
    return AnthropicCredentials(_env_file=env_file if env_file.is_file() else None)


class ApprovalSettings(BaseSettings):
    """Approval lifetimes (architecture 8.2, 8.4). Agent clock, simulated in evals."""

    model_config = SettingsConfigDict(env_prefix="ERRAND_APPROVAL_", extra="ignore")

    # A pending card lapses this long after it was requested.
    ttl_s: float = Field(default=1800.0, gt=0)
    # A granted row must be consumed within this long after the grant, or it expires.
    execute_grace_s: float = Field(default=120.0, gt=0)
    # A bound hold with less than the book timeout plus this left is not booked on
    # (9.4 step 2); M4 sends it through a fresh hold and a new card instead.
    hold_margin_s: float = Field(default=30.0, ge=0)

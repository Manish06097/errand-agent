"""SimUser: a deterministic simulated user (architecture 18.3).

It answers a question structurally, from the `fields` of the ClarifyCard it received
through the API, never by reading the question's wording, the trace or the agent's
state. It remembers every field it has given, and a card asking for any of them again
fails the scenario: that is the "never asks the same thing twice" check from the
user's side, independent of the ask guard and of INV-13.

Personas:
- cooperative: answers every asked field from its goal;
- terse: answers only the first asked field;
- evasive: dodges, giving nothing (a legitimate re-ask with options follows).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from errand.domain.brief import BriefField
from errand.domain.messages import ClarifyCard
from evals.dsl import Persona

EVASIVE_LINES = ("Not sure, you pick.", "I'd rather not say.", "Whatever works.")


class SimUserFailure(AssertionError):
    pass


@dataclass
class SimUser:
    persona: Persona
    goal: dict[BriefField, str]
    given: set[BriefField] = field(default_factory=set)
    dodges: int = 0
    violations: list[str] = field(default_factory=list)

    def said(self, gives: list[BriefField]) -> None:
        self.given |= set(gives)

    def receive(self, card: ClarifyCard) -> None:
        """Called for every ClarifyCard the user is shown."""
        again = sorted(f.value for f in card.fields if f in self.given)
        if again:
            message = f"asked again for {', '.join(again)}, which the user already gave"
            self.violations.append(message)
            raise SimUserFailure(message)

    def reply(self, card: ClarifyCard) -> tuple[str, list[BriefField]]:
        fields = list(card.fields)
        if self.persona == "evasive":
            line = EVASIVE_LINES[self.dodges % len(EVASIVE_LINES)]
            self.dodges += 1
            return line, []
        if self.persona == "terse":
            fields = fields[:1]
        known = [f for f in fields if f in self.goal]
        if not known:
            raise SimUserFailure(f"no goal value for any of {[f.value for f in fields]}")
        return ", ".join(self.goal[f] for f in known), known

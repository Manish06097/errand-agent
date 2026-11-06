from datetime import datetime
from decimal import Decimal
from enum import Enum
from pydantic import BaseModel
class Phase(str, Enum):
    COLLECTING = "collecting"; AWAITING_APPROVAL = "awaiting_approval"
class Proposal(BaseModel):
    provider_id: str; slot: datetime; price: Decimal
class State(BaseModel):
    phase: Phase = Phase.COLLECTING
    proposal: Proposal | None = None

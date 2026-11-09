"""Cancel and reschedule (N1, architecture 9.5, 10.2): the same approval, intent and
attempt flow as a booking, with no hold.

- booking handles (`B1`, `B2`, ...) for the planner's `list_bookings` view live in
  `errand.domain.booking_refs`, the way slot handles work; the model never writes a
  booking id;
- `validate`: the pure checks a `propose_cancel` passes, in the planner loop and again in
  `prepare_proposal`;
- `prepare`: the errand's kind and target, the card, the fingerprint and the pending
  approval;
- `execute`, `verify`: the consequential call and the check against `list_bookings`;
- `render`: the code templates for what comes back.

`execute.py` and `verify.py` in `agent/nodes` only dispatch here on the approval's or the
intent's action, so the booking classes stay as they are.
"""

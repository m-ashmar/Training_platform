"""Chat refusals the app branches on. `code` is the contract, never the message text."""
from django.utils.translation import gettext_lazy as _
from rest_framework import status
from rest_framework.exceptions import APIException

#: What `chat.permissions.may_message` can answer that is NOT a coaching state. The
#: coaching ones are raised as `users.exceptions.Coaching*`, so a frozen coaching reads
#: the same here as it does on a workout.
MESSAGING_REFUSALS = {
    'no_relation': _("You can only message the trainer or client you work with."),
    'blocked': _("You cannot send messages in this conversation."),
}


class MessagingRefused(APIException):
    """403 `{detail, error, code}` — the pair may not exchange messages at all.

    Distinct from the coaching refusals: those mean "not right now, and the thread is
    still readable", this one means these two are not a working pair.
    """
    status_code = status.HTTP_403_FORBIDDEN
    default_code = 'no_relation'

    def __init__(self, code):
        self.refusal_code = code if code in MESSAGING_REFUSALS else self.default_code
        super().__init__(MESSAGING_REFUSALS[self.refusal_code])

    def get_codes(self):
        return self.refusal_code


class IdempotencyConflict(APIException):
    """409 — `client_uuid` is already the id of the OTHER person's message in this
    thread. Says nothing about that message; the app picks a new id and sends again."""
    status_code = status.HTTP_409_CONFLICT
    default_code = 'idempotency_conflict'
    default_detail = _("This id belongs to another message.")

    def get_codes(self):
        return self.default_code

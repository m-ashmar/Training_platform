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


class MediaQuotaExceeded(APIException):
    """429 — the sender has put their day's allowance of photos, voice notes and videos
    into storage (`settings.CHAT_MEDIA_DAILY_COUNT` files or `CHAT_MEDIA_DAILY_BYTES`
    bytes over a rolling 24 hours; `chat.media.require_media_quota`).

    `{detail, error, code: "media_quota_exceeded", retry_after, retry_after_seconds}`
    and a `Retry-After` header — the keys the hourly limit's 429 carries. The seconds
    are until the OLDEST file in the window leaves it: the earliest moment the answer
    can change, not a promise that the next file will fit. Text messages are never
    counted and never refused by it.
    """
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    default_code = 'media_quota_exceeded'
    default_detail = _("You have sent as many photos, voice notes and videos as a day "
                       "allows. Try again later.")

    def __init__(self, seconds_left):
        super().__init__({'detail': self.default_detail})
        # After `super()`: DRF turns every value it is given into text, and the app
        # reads numbers. `wait` also makes DRF send the `Retry-After` header.
        self.wait = max(1, int(seconds_left))
        self.detail['retry_after'] = self.detail['retry_after_seconds'] = self.wait

    def get_codes(self):
        return self.default_code

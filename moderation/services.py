"""The public API of this app: what every OTHER app calls.

Four functions, and nothing in this module imports a model at import time. `social`,
`users` and `ai_assistant` all call in here, and two of them are imported BY the
reporting views to decide what a reporter may see — importing their models at the top
of this file closes that loop and breaks `manage.py check`. Models are imported inside
the functions, the way the rest of this codebase does it.

The rule the whole app exists to enforce: a block is symmetric in effect. `blocked_ids`
returns both sides of every row the person appears in, so one row hides each of them
from the other's feed, search, follower list, notifications and trainer roster. Only
the blocker can remove it.
"""
from __future__ import annotations


def _user_id(user) -> int | None:
    """The primary key of an authenticated user, or None for anyone else.

    Anonymous callers reach the list endpoints too (`GET /api/social/posts/` has an
    `AllowAny` sibling in `users`), and `AnonymousUser.pk` is None, so every caller of
    `blocked_ids` would otherwise have to guard for it.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    return user.pk


def blocked_ids(user) -> set[int]:
    """Every account `user` may not see, and that may not see `user`.

    ONE query, both directions, whatever the size of the page it filters. A set rather
    than a queryset, so a view that filters several querysets — the comment list
    filters comments AND the posts they hang off — spends one call instead of one
    correlated subquery per clause.

    Empty for an anonymous caller: there is nobody to have blocked.
    """
    from .models import Block

    from django.db.models import Q

    me = _user_id(user)
    if me is None:
        return set()
    pairs = Block.objects.filter(Q(blocker_id=me) | Q(blocked_id=me)).values_list(
        "blocker_id", "blocked_id")
    ids = {person for pair in pairs for person in pair}
    ids.discard(me)
    return ids


def is_blocked(a, b) -> bool:
    """Whether either of these two has blocked the other.

    Takes users or ids. Used by the one-to-one paths — a trainer request, a match, a
    message — where there is no queryset to filter.
    """
    from .models import Block

    from django.db.models import Q

    a_id = a if isinstance(a, int) else _user_id(a)
    b_id = b if isinstance(b, int) else _user_id(b)
    if a_id is None or b_id is None or a_id == b_id:
        return False
    return Block.objects.filter(
        Q(blocker_id=a_id, blocked_id=b_id) | Q(blocker_id=b_id, blocked_id=a_id)
    ).exists()


def open_report_count() -> int:
    """How many complaints are waiting on the operator's desk (the console badge)."""
    from .models import UNRESOLVED_STATUSES, Report

    return Report.objects.filter(
        status__in=[s.value for s in UNRESOLVED_STATUSES]).count()


# ----------------------------------------------------------------- AI consent
def has_ai_consent(user) -> bool:
    """Whether the AI coach may read this person's data.

    A missing row is a NO. Consent is a thing a person does, so the absence of the
    record cannot mean they did it — and the first release ships to accounts that have
    never been asked.
    """
    from .models import AIConsent

    me = _user_id(user)
    if me is None:
        return False
    row = AIConsent.objects.filter(user_id=me).first()
    return bool(row and row.is_active)


def require_ai_consent(user) -> None:
    """Raise `AIConsentRequired` (403, `ai_consent_required`) unless consent is live.

    THE guard. Everything that answers as the AI coach calls this one function: the
    REST surface through `moderation.permissions.HasAIConsent`, the WebSocket through
    `ai_assistant/services/chat_service.py`. Revoking takes effect on the next call
    because nothing caches the answer.
    """
    from .exceptions import AIConsentRequired

    if not has_ai_consent(user):
        raise AIConsentRequired()

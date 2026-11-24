"""One pending request (or invitation) as the TRAINER reads it.

`pending_request_row(relation, request)` is the row `trainer/pending-requests/` answers,
and the one any other trainer surface that lists a pending request must answer too (the
attention queue, BACKEND_BUGS B-091), so the two can never disagree about what a
trainer may see before they accept.

The consent boundary is the whole point of the shape (SIDE_BUGS S-154 and the directory
leak it closed): a client who ASKED has chosen to be seen by this trainer; a client who
was only invited has not, and a client either side has blocked is not shown to the
other. So there are two shapes:

- every row: `request_id`, `client_id`, `client_name`, `client_username`,
  `initiated_by`, `created_at`, and the QUOTE — what accepting will be worth to the
  trainer: `agreed_rate`, `agreed_currency`, `trainer_net`, `commission_rate`
  (B-089). The quote is the trainer's own price, so it discloses nothing about the
  client. On a trainer's own invitation, also `note`: their own words (B-093).
- a request the CLIENT sent, when neither has blocked the other, also: `client_email`,
  `client_profile_picture`, `client_age`, `client_gender`, `client_goals`,
  `client_activity_level`, `requested_at`, `status`, `note` (the client's), and the v2
  onboarding answers `training_goal`, `training_place`, `training_days_per_week`
  (B-089).

Never on a pending row, whoever asked: health notes (`specific_injury`), injury areas,
body measurements beyond what is listed above, training history. The design shows them
only after acceptance (12-trainer-shell `TrainerRequestDetail`), and the approved
trainer reads them at `trainer/client-profile/{id}/`.
"""
from __future__ import annotations

#: Where the per-request pieces are kept, so a page of rows reads the blocks and the
#: trainer's wallet once rather than once per row.
_CACHE_ATTR = '_pending_request_row_cache'


def _cached(request, key, build):
    cache = getattr(request, _CACHE_ATTR, None)
    if cache is None:
        cache = {}
        try:
            setattr(request, _CACHE_ATTR, cache)
        except AttributeError:   # a request object that takes no attributes: no cache
            return build()
    if key not in cache:
        cache[key] = build()
    return cache[key]


def quote_for_trainer(relation, request) -> dict:
    """The quoted price and what the trainer keeps of it, for the trainer reading it.

    `agreed_rate` / `agreed_currency`: the price quoted when the request was made
    (`trainer_relations.quote_agreed_price`), shown in the reader's currency when the
    operator's rate can convert it and otherwise as quoted, the unit always beside it —
    the rule every coaching price follows (`trainer_directory.CoachingPriceBook`).
    `trainer_net`: that price less the platform's commission, split by
    `users.coaching.split` at `users.coaching.commission_rate()` — the same arithmetic
    and the same rate a paid month is split with, never a percentage written here.
    `commission_rate`: that rate, as a fraction (`0.2`), so the app never hardcodes it.

    All four are null on a build that may not show a coaching fee (`can_show_price`,
    BACKEND_DELTA decision 4: the keys stay, the values go), and the first three when no
    price was quoted.
    """
    from .coaching import commission_rate, split
    from .trainer_directory import CoachingPriceBook

    prices = _cached(request, ('prices', relation.trainer_id),
                     lambda: CoachingPriceBook(request, [relation.trainer_id]))
    amount, currency = prices.agreed(relation)
    if not prices.shown:
        return {'agreed_rate': None, 'agreed_currency': None, 'trainer_net': None,
                'commission_rate': None}
    rate = commission_rate()
    net = split(amount, rate)[1] if amount is not None else None
    return {'agreed_rate': amount, 'agreed_currency': currency, 'trainer_net': net,
            'commission_rate': rate}


def pending_request_row(relation, request) -> dict:
    """The row for one pending `TrainerClientRelation`, as its trainer reads it.

    `request` is the trainer's request: it decides the build (prices) and whose blocks
    apply. `relation.client` should be `select_related`. See the module docstring for
    the two shapes and what never appears.
    """
    from moderation.services import blocked_ids
    from training_platform.media_storage import media_url

    client = relation.client
    blocked = _cached(request, ('blocked', request.user.pk),
                      lambda: blocked_ids(request.user))
    # A client who ASKED has chosen to be seen; a client who was merely invited has not
    # answered yet and must not be disclosed. Trainer-side invitations can name any
    # client id, so disclosing on this list at all made it a directory of the whole
    # client base: the consent boundary was enforced at APPROVAL and not at disclosure.
    asked_for_this = (relation.initiated_by == 'client' and client.id not in blocked)
    row = {
        'request_id': relation.id,
        'client_id': client.id,
        'client_name': client.full_name or client.username,
        'client_username': client.username,
        'initiated_by': relation.initiated_by,
        'created_at': relation.created_at,
        **quote_for_trainer(relation, request),
    }
    if relation.initiated_by == 'trainer':
        # The trainer's own words with their own invitation (B-093).
        row['note'] = relation.client_note
    if not asked_for_this:
        return row
    row.update({
        'client_email': client.email,
        # `media_url`, the one function for a stored file's URL (SIDE_BUGS S-246).
        'client_profile_picture': media_url(client.profile_picture, request) or None,
        'client_age': client.age,
        'client_gender': client.gender,
        'client_goals': client.client_goals,
        'client_activity_level': client.activity_level,
        # The v2 onboarding answers, as stored (`users.serializers.OnboardingFieldsMixin`):
        # a code, or "" / null while the client has not answered — the same values
        # `trainer/client-profile/{id}/` and `trainers/matches` send.
        'training_goal': client.training_goal,
        'training_place': client.training_place,
        'training_days_per_week': client.training_days_per_week,
        # When THIS request was sent; `created_at` for rows older than that.
        'requested_at': relation.requested_at or relation.created_at,
        'status': relation.status,
        'note': relation.client_note,
    })
    return row

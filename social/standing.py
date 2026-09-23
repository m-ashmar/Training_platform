"""Where each participant of a challenge stands — ONE rule, read by every surface.

A challenge's standing was answered three times, by three pieces of code: the stored
`ChallengeParticipation.rank` (written in a Python loop), the rows of
`GET /api/social/challenges/{id}/leaderboard/` (their position in a sliced list) and
the home card's `challenge_rank` (a subquery of its own). They were meant to agree and
nothing made them: the stored column is only as fresh as its last write, so anything
that moved a value without re-ranking — a seed, an operator's edit — left the Feed card
(`user_progress.rank`, read from that column) disagreeing with the board and the home
card on the same screen visit (BACKEND_BUGS B-086).

Now there is one definition, `place()`, and every surface annotates it:

* `ChallengeSerializer` `user_progress.rank` (the challenge list, its detail, the Feed
  card) — `ChallengeViewSet.get_queryset`;
* every row of `leaderboard/` and its `me` row — `ChallengeViewSet.leaderboard`;
* the home card's `rank` — `home/sections.py challenge_rank`;
* the stored `rank` column — `write_places`, below — which the Challenge Winner badge
  reads once the challenge is over (`achievements/engine.py challenge_wins`).

**The rule.** A participant's place is 1 + the number of participants of the same
challenge who are in the standing and strictly ahead of them. "Ahead" is the total
order `STANDING_ORDER`: more progress first; on equal progress, whoever joined earlier;
on an equal moment, the lower participation id. So two people on the same number hold
DIFFERENT places (70, 70, 48 → 1, 2, 3), and the listing order and the places are the
same order. That tiebreak is what `tests/test_side_bugs_social.py` (SIDE_BUGS S-021)
pins, and the owner's decision of 2026-09-24 — standard competition ranking, ties share
a place (70, 70, 48 → 1, 1, 3) — is owed its change here, in `_strictly_ahead`, and in
those tests, together (BACKEND_BUGS B-086 is `needs owner` until then).

**Who is in the standing.** Everyone who joined, including those still at zero, except
a participation whose `status` is in `ChallengeParticipation.NOT_IN_STANDING`
(`dropped`): it holds no place (its `place` and stored `rank` are null), it is not
listed, and it is behind nobody and ahead of nobody (SIDE_BUGS S-045).

**Cost.** `place()` is one correlated COUNT per row it is annotated on, inside the
query that reads the rows: one extra subquery for a home card, at most `limit` (50) for
a leaderboard page, one per challenge on a page of the challenge list. `write_places`
annotates every row of one challenge in ONE query and writes the rows whose place
moved in ONE bulk UPDATE; its SQL work grows with the square of the standing (each row
counts the rows ahead of it, served by the `(challenge, status, current_value)` index),
which is milliseconds at the hundreds a challenge holds today.
"""
from django.db.models import Case, Count, IntegerField, OuterRef, Q, Subquery, Value, When
from django.db.models.functions import Coalesce

#: The order the standing is LISTED in, and the order `place()` counts in. Total: no two
#: participations compare equal, so a tie is always listed the same way, earlier joiner
#: first, then the lower id.
STANDING_ORDER = ('-current_value', 'joined_at', 'id')

#: Rows per UPDATE. `bulk_update` builds one CASE arm per row, so a challenge with
#: thousands of participants is split rather than sent as one enormous statement.
WRITE_BATCH = 500


def in_standing(queryset):
    """`queryset` of participations less the ones that hold no place."""
    from .models import ChallengeParticipation

    return queryset.exclude(status__in=ChallengeParticipation.NOT_IN_STANDING)


def _strictly_ahead():
    """Participations strictly ahead of the one `OuterRef` names, by `STANDING_ORDER`."""
    return (Q(current_value__gt=OuterRef('current_value'))
            | Q(current_value=OuterRef('current_value'), joined_at__lt=OuterRef('joined_at'))
            | Q(current_value=OuterRef('current_value'), joined_at=OuterRef('joined_at'),
                id__lt=OuterRef('id')))


def place():
    """THE place of the participation row this is annotated on. See the module docstring.

    An expression over `ChallengeParticipation` rows: `.annotate(place=place())`. It
    does not ask whether the row itself is in the standing — annotate it on
    `in_standing(...)` rows, or wrap it the way `write_places` does.
    """
    from .models import ChallengeParticipation

    ahead = (in_standing(ChallengeParticipation.objects
                         .filter(challenge_id=OuterRef('challenge_id')))
             .filter(_strictly_ahead())
             .order_by().values('challenge_id').annotate(n=Count('id')).values('n'))
    return Coalesce(Subquery(ahead, output_field=IntegerField()), Value(0)) + Value(1)


def place_or_none():
    """`place()` for a row in the standing, null for one that is not (`dropped`)."""
    from .models import ChallengeParticipation

    return Case(When(status__in=ChallengeParticipation.NOT_IN_STANDING, then=Value(None)),
                default=place(), output_field=IntegerField())


def write_places(challenge) -> dict:
    """Write the stored `rank` of every participant from `place()`; return {id: rank}.

    One SELECT (every row of the challenge, its place annotated) and one bulk UPDATE of
    the rows whose place moved. A dropped participation is written NULL.

    **A challenge that is over is not re-ranked.** `lifecycle_status` is terminal once
    the end date has passed or someone closed it; from that moment the standing is a
    record, and the last ranks written while it ran are the final ones — the Challenge
    Winner badge keys on `rank=1` (SIDE_BUGS S-021, S-044).
    """
    from .models import Challenge, ChallengeParticipation

    rows = ChallengeParticipation.objects.filter(challenge=challenge)
    if challenge.lifecycle_status in Challenge.TERMINAL_STATUSES:
        return dict(rows.values_list('id', 'rank'))

    rows = list(rows.annotate(wanted=place_or_none()).only('id', 'rank', 'status'))
    moved = []
    for row in rows:
        if row.rank != row.wanted:
            row.rank = row.wanted
            moved.append(row)
    if moved:
        ChallengeParticipation.objects.bulk_update(moved, ['rank'], batch_size=WRITE_BATCH)
    return {row.pk: row.rank for row in rows}

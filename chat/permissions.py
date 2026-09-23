"""Who may write to whom — the one place the chat asks that question.

`may_message(sender, recipient)` answers None when the message may be sent, and a
refusal `code` otherwise. Every entry point — the REST send, the socket send, the
"can I type here" flag on the conversation list — goes through this function or through
`refusals_for_conversations`, which is the same rule read in bulk. There is deliberately
no second implementation: a rule that exists twice is a rule that disagrees with itself.

Today it answers with:

    `no_relation`      there is no APPROVED TrainerClientRelation between these two, and
                       they never had a paid coaching month either
    `blocked`          either of the two has blocked the other (`moderation.Block`)
    `coaching_frozen`  the renewal found the wallet short; the coaching is paused
    `coaching_unpaid`  support returned the month's payment
    `coaching_ended`   the coaching is over: no approved relation any more (ending a
                       coaching deletes it, `users.coaching.end_coaching`), but the pair
                       has paid months (`CoachingPeriod`), which outlive the relation

THE RULE FOR A COACHING THAT IS NOT PAID (the owner, 2026-09-19): frozen, unpaid and
ended keep the conversation READABLE by both sides and refuse SENDING with that code
(403). An unpaid relationship is never deleted and cannot be used, so the thread — like
the diet plan and the coach's routines — stays there to be read and stops accepting new
work. Reading is decided by membership of the conversation alone (`chat/views.py`), not
by this function.
"""
from users.coaching import refusals_for


def _blocked(sender, recipient) -> bool:
    """Has either of these two blocked the other?

    `moderation.services.is_blocked`, the one-to-one twin of `blocked_ids`: the block
    is symmetric in effect, so a block made by EITHER side closes the composer for both
    (SIDE_BUGS S-063). This stub answered False for as long as the moderation app
    existed, so a blocked person could still write — and push to the blocker's lock
    screen — while the home card already hid the coach-chat for the same pair.
    """
    from moderation.services import is_blocked

    return is_blocked(sender, recipient)


def _coached_for_money(trainer_id, client_id) -> bool:
    """Did this pair ever have a paid coaching month?

    What tells "the coaching ended" from "these two never worked together". Ending a
    coaching DELETES the relation (`users.coaching.end_coaching`), so the relation
    cannot answer; the months are money records and are kept (`CoachingPeriod`).
    """
    from users.models import CoachingPeriod

    return CoachingPeriod.objects.filter(trainer_id=trainer_id,
                                         client_id=client_id).exists()


def pair_of(a, b):
    """`(trainer, client)` for these two users, or None when they are not one of each.

    A conversation is a trainer and a client. Two trainers, two clients, an agent or an
    admin are not a pair this app knows how to open, and saying so here keeps every
    caller from re-deriving it.
    """
    if a is None or b is None or a.pk == b.pk:
        return None
    if a.is_trainer and b.is_client:
        return a, b
    if a.is_client and b.is_trainer:
        return b, a
    return None


def approved_partner_ids(user) -> set:
    """The ids this user has an APPROVED relation with, in one query."""
    from users.models import TrainerClientRelation

    if user.is_trainer:
        column, mine = 'client_id', {'trainer_id': user.pk}
    elif user.is_client:
        column, mine = 'trainer_id', {'client_id': user.pk}
    else:
        return set()
    return set(TrainerClientRelation.objects
               .filter(status='approved', **mine)
               .values_list(column, flat=True))


def may_message(sender, recipient):
    """None when `sender` may write to `recipient` right now, else the refusal `code`.

    Two questions, in this order, because the second one is only meaningful once the
    first is answered: are these two an approved trainer–client pair, and is that
    coaching in a state that may be used? Both sides ask the same question — a client
    whose coaching is frozen cannot write to their trainer, and the trainer cannot write
    to them either. Silence is mutual; the thread stays readable for both.
    """
    pair = pair_of(sender, recipient)
    if pair is None:
        return 'no_relation'
    if _blocked(sender, recipient):
        return 'blocked'
    trainer, client = pair
    if client.pk not in approved_partner_ids(trainer):
        # The relation is gone. A pair that paid for coaching gets the code the
        # module docstring promises (S-096); `no_relation` is for two people who were
        # never a working pair.
        return ('coaching_ended' if _coached_for_money(trainer.pk, client.pk)
                else 'no_relation')
    # Two queries for the whole roster, and two for one pair. Same rule as
    # `training_refusal`, which is what refuses a workout on the same coaching.
    return refusals_for(trainer.pk, [client.pk]).get(client.pk)


def may_open_thread(user, other):
    """`(trainer, client)` when `user` may open (or re-open) the thread with `other`,
    else None.

    Opening is reading, and reading outlives the coaching: an approved pair, and a pair
    whose PAID coaching has ended, may open the thread — the second one reads it and
    finds the composer closed with `coaching_ended`. Nobody opens a thread with someone
    they have blocked or who has blocked them (S-063): the start route answers the same
    404 as for a stranger, so it cannot be used to learn that a block exists.
    """
    pair = pair_of(user, other)
    if pair is None or _blocked(user, other):
        return None
    trainer, client = pair
    if other.pk in approved_partner_ids(user) or _coached_for_money(trainer.pk, client.pk):
        return pair
    return None


def refusals_for_conversations(user, conversations) -> dict:
    """`{conversation_id: code | None}` for a whole page, in a bounded number of queries.

    The conversation list shows a composer that is either open or explained, so every
    row needs this answer; asking `may_message` per row would be four queries per
    conversation. The bulk read costs one query for the approved relations, one for the
    caller's blocks (`moderation.services.blocked_ids`, both directions), at most one
    for the paid history of the rows whose relation is gone, plus two per TRAINER on the
    page — one, for a client (a client has at most one approved trainer, and every other
    thread is already refused by the relation check) and one for a trainer looking at
    their own roster.

    The same order as `may_message`, so the row and the send can never disagree:
    `blocked` first, then `coaching_ended` / `no_relation` for a pair with no approved
    relation, then the coaching's own state.
    """
    from moderation.services import blocked_ids

    rows = list(conversations)
    if not rows:
        return {}
    approved = approved_partner_ids(user)
    hidden = blocked_ids(user)
    answers, by_trainer, gone = {}, {}, []
    for conversation in rows:
        other = conversation.other_id(user.pk)
        if other is None:
            answers[conversation.pk] = 'no_relation'
            continue
        if other in hidden:
            answers[conversation.pk] = 'blocked'
            continue
        if other not in approved:
            gone.append(conversation)
            continue
        by_trainer.setdefault(conversation.trainer_id, []).append(conversation)
    if gone:
        from django.db.models import Q

        from users.models import CoachingPeriod

        # One query for every row whose relation is gone: which of these pairs paid.
        pairs = Q()
        for conversation in gone:
            pairs |= Q(trainer_id=conversation.trainer_id,
                       client_id=conversation.client_id)
        paid = set(CoachingPeriod.objects.filter(pairs)
                   .values_list('trainer_id', 'client_id').distinct())
        for conversation in gone:
            answers[conversation.pk] = (
                'coaching_ended'
                if (conversation.trainer_id, conversation.client_id) in paid
                else 'no_relation')
    for trainer_id, group in by_trainer.items():
        refusals = refusals_for(trainer_id, [c.client_id for c in group])
        for conversation in group:
            answers[conversation.pk] = refusals.get(conversation.client_id)
    return answers

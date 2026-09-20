"""Who may write to whom — the one place the chat asks that question.

`may_message(sender, recipient)` answers None when the message may be sent, and a
refusal `code` otherwise. Every entry point — the REST send, the socket send, the
"can I type here" flag on the conversation list — goes through this function or through
`refusals_for_conversations`, which is the same rule read in bulk. There is deliberately
no second implementation: a rule that exists twice is a rule that disagrees with itself.

Today it answers with:

    `no_relation`      there is no APPROVED TrainerClientRelation between these two
    `coaching_frozen`  the renewal found the wallet short; the coaching is paused
    `coaching_unpaid`  support returned the month's payment
    `coaching_ended`   the coaching is over
    `blocked`          reserved for the moderation app; `_blocked` returns False today

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

    False today: there is no blocking in this pass. The moderation app will own this
    function — a block list, its own table, and its own endpoints — and every send path
    already asks here, so wiring it up is one function body and no new call sites.
    """
    return False


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
        return 'no_relation'
    # Two queries for the whole roster, and two for one pair. Same rule as
    # `training_refusal`, which is what refuses a workout on the same coaching.
    return refusals_for(trainer.pk, [client.pk]).get(client.pk)


def refusals_for_conversations(user, conversations) -> dict:
    """`{conversation_id: code | None}` for a whole page, in a bounded number of queries.

    The conversation list shows a composer that is either open or explained, so every
    row needs this answer; asking `may_message` per row would be four queries per
    conversation. The bulk read costs one query for the approved relations plus two per
    TRAINER on the page — one, for a client (a client has at most one approved trainer,
    and every other thread is already refused by the relation check) and one for a
    trainer looking at their own roster.

    `_blocked` is NOT consulted here — it needs a bulk twin, and this is the call site
    that will want one the day the moderation app lands.
    """
    rows = list(conversations)
    if not rows:
        return {}
    approved = approved_partner_ids(user)
    answers, by_trainer = {}, {}
    for conversation in rows:
        other = conversation.other_id(user.pk)
        if other is None or other not in approved:
            answers[conversation.pk] = 'no_relation'
            continue
        by_trainer.setdefault(conversation.trainer_id, []).append(conversation)
    for trainer_id, group in by_trainer.items():
        refusals = refusals_for(trainer_id, [c.client_id for c in group])
        for conversation in group:
            answers[conversation.pk] = refusals.get(conversation.client_id)
    return answers

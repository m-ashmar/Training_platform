"""Reading the ledger as a person's activity list.

The Wallet screen (`screens/07-money/Wallet.png`) draws one row as a KIND (which picks
the icon), a readable REASON ("حجز رسوم المدرب"), who it was with ("رامي حداد"), the
date, and a SIGNED amount. A `Transaction` row carries none of those: it has
`tx_type` — one of four database words — two wallet ids and a free-form `metadata`
blob whose `purpose` the money code writes for its own use. Everything the screen
needs is derivable from those, and this module is the one place that derives it, so
the ledger view, a single transaction and anything later that shows money all read a
row the same way.

Nothing here is a price or an offer: it is a record of money that has already moved,
so it is the same on every `X-App-Channel`.

The direction is decided against the wallet the READER owns, which is why every
function takes it. The same escrow release is "your fee came back" to the client and
does not appear at all for anyone else.
"""
from __future__ import annotations

from django.utils.translation import gettext_lazy as _

#: `metadata['purpose']` -> the kind of row it is. The purposes are written by
#: `users/trainer_relations.py`, `users/coaching.py`, `wallet/signals.py` and
#: `payouts/services.py`; a purpose
#: that is not here falls through to the shape of the row itself (see `kind_of`).
PURPOSE_KINDS = {
    'trainer_request_hold': 'trainer_fee_hold',
    'coaching_renewal_charge': 'trainer_fee_hold',
    'trainer_hold_release': 'trainer_fee_refund',
    'trainer_assignment_settlement': 'trainer_fee_payout',
    'coaching_payout': 'trainer_fee_payout',
    'coaching_renewal_payout': 'trainer_fee_payout',
    'coaching_commission': 'platform_commission',
    'subscription_trainer_share': 'plan_trainer_share',
    'subscription_revenue': 'plan_payment',
    # A trainer's withdrawal (`payouts/services.py`): the amount leaving their wallet for
    # payout clearing at the request, and coming back when it fails or is cancelled.
    # The leg that takes it out of clearing once it arrived touches no person's wallet.
    'withdrawal_request': 'withdrawal',
    'withdrawal_returned': 'withdrawal_returned',
    # A routine product (routine/products.py, B-099): the buyer's one row (buyer ->
    # escrow, the whole price), the trainer's one row (escrow -> trainer, their share)
    # and the platform's commission. A sale used to be readable only as
    # `transfer_received`.
    'routine_product_charge': 'routine_product_purchase',
    'routine_product_sale': 'routine_product_sale',
    'routine_product_commission': 'platform_commission',
}

#: Purposes whose row must not name the BUYER to the trainer: a purchase is not a
#: coaching, and the buyer is not the trainer's client (`counterparty_id`).
PRODUCT_PURPOSES = frozenset({'routine_product_charge', 'routine_product_sale',
                              'routine_product_commission'})

#: kind -> what the row is called, in the reader's language. One line, no amounts and
#: no names in the string itself: the name is a separate field, because Arabic and
#: English put it in different places and a pre-built sentence cannot be laid out.
KIND_LABELS = {
    'trainer_fee_hold': _("Trainer fee held"),
    'trainer_fee_refund': _("Trainer fee returned"),
    'trainer_fee_payout': _("Coaching fee"),
    'platform_commission': _("Platform commission"),
    'plan_trainer_share': _("Your trainer's share of the plan"),
    'plan_payment': _("Plan payment"),
    'withdrawal': _("Withdrawal"),
    'withdrawal_returned': _("Withdrawal returned"),
    'plan_credit': _("Plan payment received"),
    'routine_product_purchase': _("Program purchase"),
    'routine_product_sale': _("Program sale"),
    'agent_topup': _("Cash top-up"),
    'trainer_transfer': _("Sent to a trainer"),
    'transfer_received': _("Transfer received"),
    'reversal': _("Reversed"),
    'adjustment': _("Adjustment"),
    'topup': _("Top-up"),
    'transfer': _("Transfer"),
}


def kind_of(tx, wallet) -> str:
    """The stable code the app switches its icon and its filter chip on.

    Stable is the point: `KIND_LABELS` is translated and may be reworded, and the app
    must never branch on the words. A row whose purpose is not recognised still gets
    an honest kind from its own shape rather than a wrong specific one.

    `kind_q` below is this function as a database filter (`?kind=`, B-101): a branch
    added here is a branch added there.
    """
    metadata = tx.metadata or {}
    purpose = metadata.get('purpose')
    kind = PURPOSE_KINDS.get(purpose)
    if kind == 'plan_payment' and tx.destination_wallet_id == wallet.id:
        # Only ever seen from the paying side today, but a wallet on the receiving end
        # of a plan payment is not "you paid for a plan".
        return 'plan_credit'
    if kind:
        return kind
    if tx.tx_type == 'reversal':
        return 'reversal'
    if tx.tx_type == 'topup':
        if metadata.get('agent_id'):
            return 'agent_topup'
        if metadata.get('original_payment_id'):
            return 'plan_credit'
        return 'topup'
    if tx.tx_type == 'transfer':
        if metadata.get('trainer_id') and tx.source_wallet_id == wallet.id:
            return 'trainer_transfer'
        return ('transfer_received' if tx.destination_wallet_id == wallet.id
                else 'transfer')
    return tx.tx_type if tx.tx_type in KIND_LABELS else 'adjustment'


#: Every `kind` `kind_of` can answer, in the order the contract lists them. `?kind=` on the
#: ledger routes accepts exactly these (BACKEND_BUGS B-101).
KINDS = tuple(KIND_LABELS)


def _truthy_key(key):
    """`metadata.get(key)` is truthy, as a filter: the key is there and is not JSON
    null, "", 0 or false. `has_key` comes first so a row without the key is plainly
    false (and plainly true under `~`) rather than SQL NULL, which a negation would
    silently drop."""
    from django.db.models import Q

    return (Q(metadata__has_key=key)
            & ~Q(**{f"metadata__{key}": None}) & ~Q(**{f"metadata__{key}": ""})
            & ~Q(**{f"metadata__{key}": 0}) & ~Q(**{f"metadata__{key}": False}))


def kind_q(kind, wallet):
    """`kind_of(tx, wallet) == kind` as a database filter, for `?kind=` (B-101).

    THE MIRROR OF `kind_of`: the same branches in the same order, so the ledger can be
    filtered by kind without reading every row into Python. Change one, change the other
    — `tests/test_trainer_wallet_reads.py` builds a row for every branch and fails the
    two the moment they disagree. Raises `KeyError` for a kind `kind_of` never answers.
    """
    from django.db.models import Q

    if kind not in KIND_LABELS:
        raise KeyError(kind)
    purposes = [p for p in PURPOSE_KINDS]
    known = Q(metadata__has_key='purpose') & Q(metadata__purpose__in=purposes)
    unknown = ~known
    incoming = Q(destination_wallet=wallet)

    def purpose_of(target):
        names = [p for p, k in PURPOSE_KINDS.items() if k == target]
        return Q(metadata__has_key='purpose') & Q(metadata__purpose__in=names)

    plan_payment = purpose_of('plan_payment')
    topup = unknown & Q(tx_type='topup')
    transfer = unknown & Q(tx_type='transfer')
    sent_to_trainer = _truthy_key('trainer_id') & Q(source_wallet=wallet)

    if kind == 'plan_payment':
        return plan_payment & ~incoming
    if kind == 'plan_credit':
        return ((plan_payment & incoming)
                | (topup & ~_truthy_key('agent_id') & _truthy_key('original_payment_id')))
    if kind in PURPOSE_KINDS.values():
        return purpose_of(kind)
    if kind == 'reversal':
        return unknown & Q(tx_type='reversal')
    if kind == 'agent_topup':
        return topup & _truthy_key('agent_id')
    if kind == 'topup':
        return topup & ~_truthy_key('agent_id') & ~_truthy_key('original_payment_id')
    if kind == 'trainer_transfer':
        return transfer & sent_to_trainer
    if kind == 'transfer_received':
        return transfer & ~sent_to_trainer & incoming
    if kind == 'transfer':
        return transfer & ~sent_to_trainer & ~incoming
    # 'adjustment': the one `tx_type` left, and the fallback for any other.
    return unknown & ~Q(tx_type__in=('reversal', 'topup', 'transfer'))


def direction_of(tx, wallet) -> str:
    """'in' when this row added to `wallet`, 'out' when it took away.

    A row can name the same wallet on both sides only if something moves money to
    itself, which `move_funds_atomic` refuses; destination wins either way so the
    answer is never empty.
    """
    return 'in' if tx.destination_wallet_id == wallet.id else 'out'


def ledger_owner_ids() -> set[int]:
    """The wallets that are the platform, not a person: escrow and revenue.

    They are owned by real user rows (disabled, no password) so the ledger can hold a
    balance against them, which means the naive "show the other wallet's owner" would
    put `platform_escrow` on the client's screen as the person they paid.

    ONE PER CURRENCY since the owner made both currencies first class (2026-09-21), so
    the addresses come from `wallet.utils.ledger_owner_emails` rather than from the two
    base settings — those name no row any more.
    """
    from django.contrib.auth import get_user_model

    from .utils import ledger_owner_emails

    return set(get_user_model().objects.filter(email__in=ledger_owner_emails())
               .values_list('id', flat=True))


def counterparty_id(tx, wallet, *, owner_ids=None):
    """WHICH user the row was with, without reading them.

    Split out from `counterparty_of` so a page of the ledger can resolve every
    counterparty in ONE query. It was one query per row, which is precisely the N+1
    `tests/test_regression_gate.py::test_the_ledger_does_not_cost_two_queries_per_row`
    exists to catch: 20 rows cost 21 extra queries.

    Read from `metadata` first and from the other wallet second. Escrow is the other
    wallet for every coaching hold, release and payout, and escrow is not who the
    money is with — the trainer is, and `metadata['trainer_id']` is the only place
    that says so.
    """
    metadata = tx.metadata or {}
    owner_ids = ledger_owner_ids() if owner_ids is None else owner_ids

    if metadata.get('trainer_id') and metadata.get('trainer_id') != wallet.owner_id:
        return metadata['trainer_id']
    if (metadata.get('purpose') in PRODUCT_PURPOSES or metadata.get('purchase_id')) \
            and metadata.get('trainer_id') == wallet.owner_id:
        # The trainer's side of a routine-product sale or its refund: the other wallet
        # is escrow or, for an exchange leg, nobody, and the buyer is never named.
        return None
    if metadata.get('client_id') and metadata.get('client_id') != wallet.owner_id:
        return metadata['client_id']
    if metadata.get('agent_id'):
        return metadata['agent_id']
    other = (tx.source_wallet if tx.destination_wallet_id == wallet.id
             else tx.destination_wallet)
    if other is not None and other.owner_id not in owner_ids:
        return other.owner_id
    return None


def counterparty_users(ids):
    """`{id: user}` for a whole page's counterparties, in one query.

    `agent_profile` comes with them: an agent is shown by their shop's name, and
    fetching that per row would put the N+1 straight back.
    """
    from django.contrib.auth import get_user_model

    ids = {i for i in ids if i}
    if not ids:
        return {}
    return {u.id: u for u in get_user_model().objects
            .filter(pk__in=ids)
            .select_related('agent_profile')
            .only('id', 'username', 'first_name', 'last_name', 'user_type',
                  'agent_profile__display_name', 'agent_profile__user')}


def describe(user) -> dict | None:
    """One counterparty as the row shows them."""
    if user is None:
        return None
    name = (user.full_name or user.username)
    if user.user_type == 'agent':
        # An agent is a shop, and the shop's name is what the client was told to look
        # for. Falls back to the person's name when the listing has none.
        listing = getattr(user, 'agent_profile', None)
        name = getattr(listing, 'display_name', '') or name
    return {'id': user.id, 'name': name, 'role': user.user_type}


def counterparty_of(tx, wallet, *, owner_ids=None, users=None) -> dict | None:
    """Who the row was with: `{"id", "name", "role"}`, or None when it was nobody.

    `users` is a `{id: user}` map for the whole page (`counterparty_users`); without
    one this reads the single user itself, which is right for a detail view and wrong
    for a list.
    """
    user_id = counterparty_id(tx, wallet, owner_ids=owner_ids)
    if not user_id:
        return None
    if users is not None:
        return describe(users.get(user_id))
    return describe(counterparty_users([user_id]).get(user_id))


#: The `metadata` keys a PERSON is served on their own ledger rows (BACKEND_BUGS B-184).
#: Everything else in the stored blob is the money code's bookkeeping — `purpose` (read
#: it as `kind`), other accounts' ids (`client_id`, `trainer_id`, `agent_id`: the
#: `counterparty` names who the row was with), `purchase_id`, `relation_id`,
#: `period_id`, `hold_reference`, the transfer markers — or an OPERATOR's words (a
#: reversal's or a refund's `reason`, written for support, never for the customer).
#: These are the keys the app reads, and the reversed row's own reference.
READER_METADATA_KEYS = frozenset({
    "title", "product_title", "routine_name", "product_id", "month",
    "withdrawal_id", "withdrawal_number", "original_reference",
})


def reader_metadata(metadata) -> dict:
    """The part of a row's stored `metadata` its owner is served (`READER_METADATA_KEYS`)."""
    if not isinstance(metadata, dict):
        return {}
    return {key: value for key, value in metadata.items() if key in READER_METADATA_KEYS}


def reason_of(kind: str) -> str:
    label = KIND_LABELS.get(kind)
    return str(label) if label is not None else kind

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
#: `users/trainer_relations.py`, `users/coaching.py` and `wallet/signals.py`; a purpose
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
}

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
    'plan_credit': _("Plan payment received"),
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
    """
    from django.conf import settings
    from django.contrib.auth import get_user_model

    emails = [getattr(settings, 'PLATFORM_ESCROW_EMAIL', 'platform_escrow@local'),
              getattr(settings, 'PLATFORM_REVENUE_EMAIL', 'platform_revenue@local')]
    return set(get_user_model().objects.filter(email__in=emails)
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


def reason_of(kind: str) -> str:
    label = KIND_LABELS.get(kind)
    return str(label) if label is not None else kind

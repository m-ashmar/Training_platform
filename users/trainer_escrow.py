"""What the platform holds in escrow FOR a trainer, read from the trainer's side
(BACKEND_BUGS B-096).

TrainerEarnings (`screens/15-trainer-money`) draws «محجوز لدى المنصة · 3 عملاء بانتظار
أول برنامج»: the coaching money a client has already paid into escrow for month 1, which
the trainer receives when they send the first program (`users.trainer_relations
.settle_hold`). The client's wallet read has said this since B-047 (`held`, `held_for`);
the trainer's said nothing, so the app multiplied the trainer's CURRENT price by a count
of clients without a routine — neither the right price nor the right clients.

A HOLD IS, EXACTLY, an APPROVED `TrainerClientRelation` whose `hold_reference` names a
client -> escrow ledger row that is still held: its `CoachingPeriod` (same
`hold_reference`) is `held`, and no reversal of that row exists. The relation names the
live hold (`trainer_relations.py`: "One hold, named here"); the period carries the price
of record, the currency it was charged in and the commission rate copied onto it when it
was charged. A hold from before months existed has no period: its ledger row gives the
amount and currency, and today's `commission_rate()` is the rate — `settle_hold` opens
its month at settlement with exactly that rate. A renewal is never HELD: `renew` charges
and pays out in one transaction.

BUT A PAID MONTH CAN STILL BE OWED TO THE TRAINER (the payouts review, 2026-09-24). When
the pair trades in two currencies and the operator's rate is missing or stale at the
moment `users.coaching.pay_out` pays a month — month 1 at the first program, or any
renewal — the client's charge and the platform's commission go through, the month runs,
and the trainer's share stays in escrow, OWED (`CoachingPeriod.payout_owed_since`), until
`settle_owed_payouts` finds a fresh rate. That share is neither held (the relation has
forgotten the hold and the month is `settled`) nor a ledger row of the trainer's, so it
appeared in no figure at all for as long as the rate stayed stale. `owed_to_trainer`
reads it as its OWN figure: `held_for_trainer` keeps meaning month 1 waiting for a first
program, and nothing owed is added to it.

A hold an operator already reversed (`POST /api/wallet/admin/reversal/`) is NOT held: the
money went back to the client, and `_move_hold` will find the reversal and forget the
reference instead of paying it. Counting it would promise the trainer money that left.

EVERY AMOUNT IS WHAT THE TRAINER WILL RECEIVE, IN THE TRAINER'S CURRENCY — the currency of
the wallet being read. `users.coaching.pay_out` splits the month's price with
`users.coaching.split` (commission to the cent, ROUND_HALF_UP; the trainer takes the
remainder) in the month's currency, which is the CLIENT's. When the trainer trades in the
same currency that split is exact. When they trade in the other one, `pay_out` converts
the trainer's share at the operator's rate ON THE DAY IT PAYS, so the figure here is that
conversion at today's rate (`wallet.exchange.price_list_converter`, the read-only form of
the one conversion boundary) and the item says `estimated: true`. With no usable rate the
trainer's price of record is used (the quote, `price_amount`, when it is in their
currency), and failing that the item stays in the month's own currency and is left out of
the two totals rather than added across units.

Nothing here writes, locks or moves money, and no ledger row is invented: the ledger is
append-only double entry, and a hold is ONE row, client -> escrow, that is not the
trainer's. The items are shaped so the app can draw the design's «حجز رسوم تدريب» row
beside the trainer's real rows; they carry no `reference_id` because they are not rows.
"""
from __future__ import annotations

from decimal import Decimal

ZERO = Decimal('0.00')

#: The status every item carries: the same word `GET /api/v1/coaching/current/` answers the
#: client for the same month (`users.coaching.coaching_status`).
AWAITING_FIRST_PROGRAM = 'awaiting_first_program'

#: The status of a paid month whose trainer's share is still owed in escrow.
PAYOUT_OWED = 'payout_owed'


def held_for_trainer(trainer_id, currency) -> dict:
    """`{'net', 'gross', 'count', 'items'}` for the trainer `trainer_id`, in `currency`
    (the trainer's wallet's).

    `net` and `gross` are Decimals summed over the items in `currency`; `count` is every
    client whose month 1 is held (the design's «3 عملاء بانتظار أول برنامج»), including
    one whose amount could not be expressed in `currency`. `items`, newest hold first
    (ties by relation id, so the order is total), each:

        relation_id, client (`wallet.activity.describe`), status, kind, month,
        requested_at, held_at, amount, commission, commission_rate, net, currency,
        charged_amount, charged_currency, estimated

    `amount` = `commission` + `net` (the client's fee, the platform's cut, what the trainer
    receives), all in `currency` (the item's). `charged_*` is the month as the client paid
    it. Five queries at most, whatever the number of clients.
    """
    from wallet.models import Transaction

    from .coaching import commission_rate
    from .models import CoachingPeriod, TrainerClientRelation

    empty ={'net': ZERO, 'gross': ZERO, 'count': 0, 'items': []}
    relations = list(TrainerClientRelation.objects
                     .filter(trainer_id=trainer_id, status='approved')
                     .exclude(hold_reference='')
                     .select_related('client')
                     .order_by('pk'))
    if not relations:
        return empty
    refs = [r.hold_reference for r in relations]
    periods = {p.hold_reference: p
               for p in CoachingPeriod.objects.filter(hold_reference__in=refs)}
    reversed_refs = set(Transaction.objects
                        .filter(reverses__reference_id__in=refs)
                        .values_list('reverses__reference_id', flat=True))
    legacy = [ref for ref in refs if ref not in periods and ref not in reversed_refs]
    legacy_rows = ({t.reference_id: t for t in
                    Transaction.objects.filter(reference_id__in=legacy)}
                   if legacy else {})

    converter = []
    items = []
    for relation in relations:
        ref = relation.hold_reference
        if ref in reversed_refs:
            continue
        period = periods.get(ref)
        if period is not None:
            if period.status != 'held':
                continue
            price, charged_in, rate = period.price, period.currency, period.commission_rate
            month, held_at = period.number, period.created_at
            record = (period.price_amount, period.price_currency)
        else:
            row = legacy_rows.get(ref)
            if row is None:
                continue
            price, charged_in, rate = row.amount, row.currency, commission_rate()
            month, held_at = 1, row.created_at
            record = (None, '')
        amount, commission, net, unit, estimated = _as_the_trainer_is_paid(
            Decimal(price), charged_in, Decimal(rate), record, currency, converter)
        items.append({
            'relation_id': relation.pk,
            'client': _describe(relation.client),
            'status': AWAITING_FIRST_PROGRAM,
            'kind': 'trainer_fee_hold',
            'month': month,
            'requested_at': relation.requested_at,
            'held_at': held_at,
            'amount': amount,
            'commission': commission,
            'commission_rate': Decimal(rate),
            'net': net,
            'currency': unit,
            'charged_amount': Decimal(price),
            'charged_currency': charged_in,
            'estimated': estimated,
        })

    items.sort(key=lambda item: (item['held_at'], item['relation_id']), reverse=True)
    summable = [item for item in items if item['currency'] == currency]
    return {'net': sum((i['net'] for i in summable), ZERO),
            'gross': sum((i['amount'] for i in summable), ZERO),
            'count': len(items),
            'items': items}


def owed_to_trainer(trainer_id, currency) -> dict:
    """`{'net', 'items'}`: every paid month whose trainer's share `pay_out` left OWED in
    escrow for the trainer `trainer_id`, in `currency` (the trainer's wallet's). See the
    module docstring.

    `net` is a Decimal summed over the items in `currency`. `items`, newest owed first
    (ties by period id, so the order is total), each:

        period_id, relation_id, client (`wallet.activity.describe`), status
        (`payout_owed`), kind (`trainer_fee_payout`, the ledger's kind for the row that
        arrives when it is paid), month, owed_since, starts_at, amount, commission,
        commission_rate, net, currency, charged_amount, charged_currency, estimated

    The amounts are the month's own price and commission rate expressed as the trainer
    will be paid, exactly as `held_for_trainer` expresses a held month: today's rate, or
    the price of record in the trainer's currency (a renewal inherits month 1's), or —
    with neither — the month's own currency, left out of `net`. Two queries at most (the
    months with their clients, and today's rate once), whatever the number of months.
    """
    from .models import CoachingPeriod

    periods = (CoachingPeriod.objects
               .filter(trainer_id=trainer_id, payout_owed_since__isnull=False)
               .select_related('client')
               .order_by('-payout_owed_since', '-pk'))
    converter = []
    items = []
    for period in periods:
        amount, commission, net, unit, estimated = _as_the_trainer_is_paid(
            Decimal(period.price), period.currency, Decimal(period.commission_rate),
            (period.price_amount, period.price_currency), currency, converter)
        items.append({
            'period_id': period.pk,
            'relation_id': period.relation_id,
            'client': _describe(period.client),
            'status': PAYOUT_OWED,
            'kind': 'trainer_fee_payout',
            'month': period.number,
            'owed_since': period.payout_owed_since,
            'starts_at': period.starts_at,
            'amount': amount,
            'commission': commission,
            'commission_rate': Decimal(period.commission_rate),
            'net': net,
            'currency': unit,
            'charged_amount': Decimal(period.price),
            'charged_currency': period.currency,
            'estimated': estimated,
        })
    return {'net': sum((i['net'] for i in items if i['currency'] == currency), ZERO),
            'items': items}


def _as_the_trainer_is_paid(price, charged_in, rate, record, currency, converter):
    """`(amount, commission, net, unit, estimated)` for one held month, as `pay_out` will
    pay it. See the module docstring for the three cases."""
    from wallet.exchange import price_list_converter

    from .coaching import split

    commission, share = split(price, rate)
    if charged_in == currency:
        return price, commission, share, currency, False
    if not converter:
        converter.append(price_list_converter(currency))
    convert = converter[0]
    net, net_unit = convert(share, charged_in)
    fee, fee_unit = convert(commission, charged_in)
    if net_unit == currency and fee_unit == currency:
        return net + fee, fee, net, currency, True
    quoted, quoted_in = record
    if quoted is not None and quoted_in == currency:
        fee, net = split(Decimal(quoted), rate)
        return Decimal(quoted), fee, net, currency, True
    return price, commission, share, charged_in, True


def _describe(user):
    from wallet.activity import describe

    return describe(user)

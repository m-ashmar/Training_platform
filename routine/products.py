"""Routine products: review, sale and refund (plan step 10; the payments spec, section 8
"Routine products"; the models are `routine/product_models.py`).

WHAT IS SOLD, AND ON WHICH BUILD. A product is ROUTINE_PRODUCT money: a digital good
sold one-to-many, which both stores reserve for their own billing. So the purchase
endpoint asks `policy.services.require_purchase_allowed(request, ROUTINE_PRODUCT)` before
anything else, and that refuses `play` and `ios` in code (`not_available_on_channel`);
a store build is not shown a price or a buy action either (`can_show_price`,
`routine/product_serializers.py`). OWNING works everywhere: the buyer's copy is an
ordinary routine of theirs, readable and trainable on every build.

THE SALE (`buy`) is one transaction, idempotent on the buyer's key:

    lock the product row                       (FOR UPDATE: one sale of it at a time)
    replay?  a purchase with this key          -> that purchase, nothing moves
    refuse   owned / not on sale / rate stale  -> nothing moves
    write the buyer's copy                     (`routine_from_template`, no money in it)
    lock the buyer's user row, then every wallet in ascending id   (`users.coaching.lock_wallets`)
    buyer  -> escrow            the price, in the buyer's currency    'routine_product_charge'
    escrow -> trainer           the trainer's share                   'routine_product_sale'
    escrow -> platform revenue  the commission                        'routine_product_commission'

All three rows are ROUTINE_PRODUCT. Escrow is the clearing account for the one
transaction and nets to zero in it, so the net movement is exactly the spec's — buyer ->
trainer (the share) + platform revenue (the commission) — while each person reads ONE
row, and neither row names the other person's wallet: the trainer is not told who bought
(the buyer is not their client; `Transaction.source_owner` would otherwise hand them
the buyer's username), and the buyer sees the trainer as the counterparty. It is the
shape of a coaching payout (`users/coaching.pay_out`), including where currencies
cross: the price of record is converted into the buyer's currency by
`subscription.pricing.charge_for` (the rate snapshotted on the purchase), and a share
that has to reach a trainer in the other currency leaves escrow as an explicit exchange
(`wallet.models.exchange_funds_atomic`) — the only two places money changes unit
(`wallet/exchange.py`). A stale or missing rate refuses the sale before anything moves.

The commission is `COACHING_COMMISSION_RATE` (the spec: it applies to ROUTINE_PRODUCT
sales), read when the sale is made and copied onto the purchase, split by the same
`users.coaching.split` coaching uses, so the two parts always add up to the price paid.

THE REFUND (`refund`, operator only) reverses every leg with `reverses=` — the ledger's
own at-most-once guard — at the amounts and the rate of the sale, in one transaction,
and REVOKES the buyer's copy (unassigned and retired; their training history on it is
kept, like any unassigned routine's). Revoked, not frozen, although the owner's freeze
rule keeps an unpaid coaching's routines readable: there the routine is the record of
a service that was delivered, while here the program IS the product — a refunded buyer
who kept reading it would hold everything they were refunded for, and the free preview
(`preview_days`) would mean nothing. A training write against a refunded copy answers
403 `product_refunded` (`users.coaching.training_refusal`). If the trainer's wallet no
longer holds their share, nothing moves and the operator is told why.

Every push goes out after commit (`transaction.on_commit`), states the fact and carries
no price, button or link: a push reaches every build (spec section 7).

BOUGHT THROUGH A CHECKOUT (the web payment hub, the spec's section 5;
`api/v1/checkout/sessions/`). The same product can be paid by ShamCash instead of the
wallet. The checkout is a `subscription.Payment` (money_type ROUTINE_PRODUCT, `buyer`,
`routine_product`) that `PaymentService.start_product_checkout` opens under this
product's row lock — priced by `checkout_terms`, which also snapshots the commission
rate — and that the pollers, the webhook, "check my payment" and the review queue
confirm through `PaymentService.complete_payment`, exactly as a plan's. Confirmation
delivers it here, in the completion's own transaction (`grant_checkout`):

    lock the product row          (the caller holds the payment's: payment, then product)
    a purchase, paid_with=shamcash, checkout_reference = the checkout's reference
    write the buyer's copy
    lock the buyer's user row, then every wallet in ascending id
    nothing -> escrow           what arrived, in the checkout's currency 'routine_product_checkout'
    escrow  -> trainer          the trainer's share                      'routine_product_sale'
    escrow  -> platform revenue the commission                           'routine_product_commission'

The split is the wallet sale's (`_pay_out_of_escrow`, `users.coaching.split`), at the
rate snapshotted when the checkout opened, and a share that crosses currencies is
exchanged at the rate of that same moment. The buyer's wallet is not touched — the money
came from ShamCash, not from it — so the money enters the ledger in escrow, the way a
plan payment's enters in the client's wallet (`wallet/signals.py`). A checkout that
expires or is rejected delivers nothing; a program that cannot be delivered when the
money arrives (`checkout_refusal`: owned already, or no longer deliverable) is not
delivered and the money is held for an operator.

Refunding such a sale goes through `PaymentService.refund_payment` whichever door the
operator uses (the payment, or this purchase in the console): the payment moves to
`refunded` with its event, the sale's legs are reversed as a wallet sale's are, and the
money that entered escrow leaves it again — handed back by hand, as a plan refund's.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from policy.money import MoneyType

from .product_models import (
    MAX_PREVIEW_DAYS, PaidWith, ProductStatus, PurchaseStatus, ReviewDecision,
    RoutineProduct, RoutinePurchase,
)

logger = logging.getLogger(__name__)

#: The longest program a product may sell, in training days — a routine of the buyer's
#: gets one day per session, and a day costs about five queries to scaffold.
MAX_PROGRAM_DAYS = 365
#: The longest reason an operator may give; the trainer is shown it.
MAX_REASON = 1000
#: The purchase key a checkout's delivery writes (`grant_checkout`: this prefix and the
#: payment's id). A buyer's own keys share the column and its per-buyer uniqueness
#: (`routine_purchase_key_per_buyer`), so no key a buyer sends may start with it
#: (`BuySerializer`, field code `idempotency_key_reserved`). Otherwise a wallet buy
#: keyed `checkout:<their session id>` made that checkout's delivery raise
#: IntegrityError inside the completion, and every poller run stopped at that row:
#: nobody's checkout after it was confirmed or expired (SIDE_BUGS S-234).
CHECKOUT_KEY_PREFIX = "checkout:"


# ---------------------------------------------------------------- refusals
class ProductRefusal(Exception):
    """A refusal the API answers with `code` and `status` (and `extra` in the body)."""

    code = "product_refused"
    status = 409

    def __init__(self, message="", **extra):
        super().__init__(message or self.code)
        self.extra = extra


class ProductNotEditable(ProductRefusal):
    code = "product_not_editable"


class ProductNotSubmittable(ProductRefusal):
    code = "product_not_submittable"


class ProductIncomplete(ProductRefusal):
    code = "product_incomplete"


class ProgramTooLong(ProductRefusal):
    code = "program_too_long"


class PreviewCoversProgram(ProductRefusal):
    code = "preview_covers_program"


class ProductAlreadyRetired(ProductRefusal):
    code = "product_already_retired"


class TrainerNotVerified(ProductRefusal):
    code = "trainer_not_verified"
    status = 403


class ProductNotForSale(ProductRefusal):
    code = "product_not_for_sale"


class AlreadyOwned(ProductRefusal):
    code = "already_owned"


class IdempotencyKeyReused(ProductRefusal):
    code = "idempotency_key_reused"
    status = 422


class CheckoutOpen(ProductRefusal):
    """A wallet purchase of a product the buyer has a checkout open for: paying both
    would pay twice. `payment_id` is the checkout."""
    code = "checkout_open"


class ReviewConflict(Exception):
    """An operator decision that cannot be taken; the message is for the console."""


# ---------------------------------------------------------------- reading
def program_days(template) -> int:
    """How many training days the buyer's copy has: the template's session count when
    the trainer has said how many weeks it runs (`RoutineTemplate.session_count`),
    otherwise one pass through its days."""
    if template is None:
        return 0
    return template.session_count or template.days or 0


def net_of(price, rate=None):
    """`(commission, net)` of `price` at `rate` (default: today's commission rate) —
    what the trainer's form shows beside the price. `users.coaching.split`, the same
    arithmetic every sale is made with."""
    from users.coaching import commission_rate, split

    if price is None:
        return None, None
    return split(Decimal(price), commission_rate() if rate is None else rate)


def catalogue_for(user):
    """The products `user` may be offered: published, still backed by the template
    version their review saw, by a trainer who is still verified (`is_on_sale`),
    publicly visible, and has not blocked (or been blocked by) `user`."""
    from moderation.services import blocked_ids
    from users.visibility import public_q

    queryset = (RoutineProduct.objects
                .filter(status=ProductStatus.PUBLISHED, source_template__isnull=False,
                        source_template__updated_at=F("template_version"),
                        trainer__trainer_is_verified=True)
                .filter(public_q("trainer")))
    blocked = blocked_ids(user)
    if blocked:
        queryset = queryset.exclude(trainer_id__in=blocked)
    return queryset


def owned_purchase(user, product) -> RoutinePurchase | None:
    """The buyer's confirmed purchase of `product`, if they own it."""
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    return (RoutinePurchase.objects
            .filter(buyer=user, product=product, status=PurchaseStatus.CONFIRMED)
            .select_related("routine").first())


def open_checkout_for(user, product):
    """The buyer's open checkout (`subscription.Payment`) for `product`, if any."""
    from subscription.models import OPEN_PAYMENT_STATUSES, Payment

    return (Payment.objects.filter(buyer=user, routine_product=product,
                                   status__in=OPEN_PAYMENT_STATUSES)
            .order_by("created_at", "id").first())


def visible_product(user, product_id) -> RoutineProduct | None:
    """The product with this id if `user` may read it: its trainer, a buyer who owns it
    (a retired product stays readable to them), or anyone it is offered to. None
    otherwise — the same 404 whether it does not exist or is not theirs to see."""
    product = (RoutineProduct.objects.select_related("trainer", "source_template", "cover")
               .filter(pk=product_id).first())
    if product is None:
        return None
    if product.trainer_id == user.pk or owned_purchase(user, product) is not None:
        return product
    if catalogue_for(user).filter(pk=product.pk).exists():
        return product
    return None


def preview_of(product) -> list[dict]:
    """The first `preview_days` days of the program as the buyer's copy would hold them:
    program day N is template day ((N-1) % template.days) + 1, exactly as
    `routine_from_template` writes it. Content, not a price, so it is the same on every
    build."""
    template = product.source_template
    if template is None or not product.preview_days:
        return []
    cycle = template.days or 1
    # Never a whole cycle — that is every distinct row the paid copy holds. `submit`
    # refuses such a product; this also holds for one published before that rule.
    days = min(product.preview_days, program_days(template), max(cycle - 1, 0))
    rows = list(template.routinetemplateexercise_set.select_related("exercise")
                .order_by("day", "order", "id"))
    out = []
    for day in range(1, days + 1):
        template_day = ((day - 1) % cycle) + 1
        out.append({
            "day": day,
            "exercises": [
                {"exercise_id": row.exercise_id, "name": row.exercise.name,
                 "target_muscle": row.exercise.target_muscle, "sets": row.sets,
                 "reps": row.reps, "rest_time": row.rest_time, "order": row.order}
                for row in rows if row.day == template_day],
        })
    return out


# ---------------------------------------------------------------- the trainer
def lock_own(trainer, product_id) -> RoutineProduct:
    """The trainer's product, locked for a change. `RoutineProduct.DoesNotExist` when it
    is not theirs."""
    return (RoutineProduct.objects.select_for_update(of=("self",))
            .select_related("source_template").get(pk=product_id, trainer=trainer))


def submit(trainer, product_id) -> RoutineProduct:
    """Send a product to the operators' review queue.

    Allowed from DRAFT, again from IN_REVIEW (the trainer changed the program while it
    waited, and this re-reads it), and from PUBLISHED when the template has changed
    since its review (the product is off sale until an operator sees the new version).
    Only a VERIFIED trainer may submit; anyone may keep drafts. Records the template
    version the review will see.
    """
    from users.models import CustomUser

    with transaction.atomic():
        product = lock_own(trainer, product_id)
        resubmit = product.status == ProductStatus.PUBLISHED and product.template_changed
        if product.status not in (ProductStatus.DRAFT, ProductStatus.IN_REVIEW) and not resubmit:
            raise ProductNotSubmittable(status=product.status)
        verified = (CustomUser.objects.filter(pk=trainer.pk)
                    .values_list("trainer_is_verified", flat=True).first())
        if not verified:
            raise TrainerNotVerified()
        template = product.source_template
        missing = [name for name, present in (
            ("title_ar", bool(product.title_ar.strip())),
            ("title_en", bool(product.title_en.strip())),
            ("price", product.price is not None),
            ("template", template is not None),
        ) if not present]
        if template is not None and not template.routinetemplateexercise_set.exists():
            missing.append("template_exercises")
        if missing:
            raise ProductIncomplete(missing=missing)
        days = program_days(template)
        if days > MAX_PROGRAM_DAYS:
            raise ProgramTooLong(max_days=MAX_PROGRAM_DAYS, program_days=days)
        # The program is the template's week REPEATED (`routine_from_template`), so a
        # preview as long as that week is the whole program, however many weeks it runs:
        # it must stay shorter than the cycle, not the program (the review, 2026-09-24).
        cycle = template.days or 1
        if product.preview_days and product.preview_days >= min(days, cycle):
            raise PreviewCoversProgram(program_days=days, cycle_days=cycle)
        product.status = ProductStatus.IN_REVIEW
        product.submitted_at = timezone.now()
        product.template_version = template.updated_at
        product.save(update_fields=["status", "submitted_at", "template_version", "updated_at"])
        return product


def retire(trainer, product_id) -> RoutineProduct:
    """Take a product off sale for good. Buyers keep their copies."""
    with transaction.atomic():
        product = lock_own(trainer, product_id)
        if product.status == ProductStatus.RETIRED:
            raise ProductAlreadyRetired()
        product.status = ProductStatus.RETIRED
        product.retired_at = timezone.now()
        product.save(update_fields=["status", "retired_at", "updated_at"])
        return product


# ---------------------------------------------------------------- the operator
def _lock_for_review(product_id) -> RoutineProduct:
    return (RoutineProduct.objects.select_for_update(of=("self",))
            .select_related("source_template", "trainer").get(pk=product_id))


def approve(product_id, operator) -> RoutineProduct:
    """IN_REVIEW -> PUBLISHED. Refused when the program changed after it was submitted
    (the operator would be approving a version they did not see) or the trainer is no
    longer verified."""
    with transaction.atomic():
        product = _lock_for_review(product_id)
        if product.status != ProductStatus.IN_REVIEW:
            raise ReviewConflict(f"This product is {product.status}, not in review: it was "
                                 f"decided already or withdrawn.")
        if product.source_template is None or product.template_changed:
            raise ReviewConflict("The trainer changed the program after submitting it. "
                                 "Reject it with a reason so they resubmit the new version.")
        if not product.trainer.trainer_is_verified:
            raise ReviewConflict("The trainer is not verified any more.")
        now = timezone.now()
        product.status = ProductStatus.PUBLISHED
        product.review_decision = ReviewDecision.APPROVED
        product.review_reason = ""
        product.reviewed_at, product.reviewed_by = now, operator
        product.published_at = now
        product.save(update_fields=["status", "review_decision", "review_reason", "reviewed_at",
                                    "reviewed_by", "published_at", "updated_at"])
        _notify_on_commit("approved", product)
        return product


def reject(product_id, operator, reason) -> RoutineProduct:
    """IN_REVIEW -> DRAFT with the operator's reason, which the trainer is shown."""
    reason = (reason or "").strip()
    if not reason:
        raise ReviewConflict("Say why the product is rejected: the trainer is shown this "
                             "reason.")
    if len(reason) > MAX_REASON:
        raise ReviewConflict(f"The reason is too long: at most {MAX_REASON} characters.")
    with transaction.atomic():
        product = _lock_for_review(product_id)
        if product.status != ProductStatus.IN_REVIEW:
            raise ReviewConflict(f"This product is {product.status}, not in review: it was "
                                 f"decided already or withdrawn.")
        product.status = ProductStatus.DRAFT
        product.review_decision = ReviewDecision.REJECTED
        product.review_reason = reason
        product.reviewed_at, product.reviewed_by = timezone.now(), operator
        product.save(update_fields=["status", "review_decision", "review_reason", "reviewed_at",
                                    "reviewed_by", "updated_at"])
        _notify_on_commit("rejected", product, reason=reason)
        return product


# ---------------------------------------------------------------- the sale
def _write_copy(product, buyer, purchase):
    """The buyer's own copy of the program: `routine_from_template` (which moves no
    money), named after the product in both languages, and assigned to the buyer."""
    from django.conf import settings

    from .assignment import routine_from_template

    default = settings.LANGUAGE_CODE
    routine = routine_from_template(
        product.source_template, created_by=buyer, days=program_days(product.source_template),
        name=product.title_in(default), source=False, purchase=purchase)
    translations = {}
    for language, _label in settings.LANGUAGES:
        if language != default:
            translations[language] = {"name": product.title_in(language),
                                      "description": product.description_in(language)}
    routine.name = product.title_in(default)
    routine.description = product.description_in(default)
    routine.translations = translations
    routine.save(update_fields=["name", "description", "translations", "updated_at"])
    routine.assigned_to.add(buyer)
    return routine


def buy(product_id, buyer, *, idempotency_key) -> tuple[RoutinePurchase, bool]:
    """Sell one product to `buyer` from their wallet. Returns `(purchase, created)`;
    `created` is False for a replay of the same key.

    Raises `ProductNotForSale`, `AlreadyOwned`, `CheckoutOpen`, `IdempotencyKeyReused`,
    `wallet.exchange.ExchangeRateUnavailable`, `wallet.models.InsufficientBalance`, or a
    `ValueError` the ledger raised. Nothing moves on any of them. The CALLER has
    already asked `require_purchase_allowed`; see the module docstring for the order.
    """
    from subscription.pricing import charge_for
    from users.coaching import commission_rate, lock_wallets, split
    from users.trainer_relations import _wallet_for
    from wallet.models import InsufficientBalance, currency_for, move_funds_atomic
    from wallet.utils import get_escrow_wallet, get_platform_revenue_wallet

    with transaction.atomic():
        product = (RoutineProduct.objects.select_for_update(of=("self",))
                   .select_related("source_template", "trainer").filter(pk=product_id).first())
        if product is None:
            raise ProductNotForSale()
        replay = RoutinePurchase.objects.filter(buyer=buyer, idempotency_key=idempotency_key).first()
        if replay is not None:
            if replay.product_id != product.pk:
                raise IdempotencyKeyReused()
            return replay, False
        owned = owned_purchase(buyer, product)
        if owned is not None:
            raise AlreadyOwned(purchase_id=owned.pk,
                               routine_id=getattr(getattr(owned, "routine", None), "pk", None))
        # A checkout of this product is open for this buyer (the web hub): they may be
        # transferring against its reference right now, and paying both would pay twice.
        # Decided under the product lock, which is the lock the checkout opens under.
        checkout = open_checkout_for(buyer, product)
        if checkout is not None:
            raise CheckoutOpen(payment_id=str(checkout.pk))
        if not product.is_on_sale:
            raise ProductNotForSale()

        buyer_currency = currency_for(buyer)
        priced = charge_for(product.price, product.currency, buyer_currency)
        rate = commission_rate()
        commission, share = split(priced.amount, rate)
        buyer_wallet = _wallet_for(buyer, "client")
        if buyer_wallet.balance < priced.amount:
            # Early and unlocked, so a buyer who cannot pay costs no copy; the ledger
            # checks again under the lock.
            raise InsufficientBalance()

        try:
            with transaction.atomic():
                purchase = RoutinePurchase.objects.create(
                    buyer=buyer, product=product, status=PurchaseStatus.CONFIRMED,
                    price_amount=product.price, price_currency=product.currency,
                    amount=priced.amount, currency=buyer_currency,
                    exchange_rate=priced.rate,
                    exchange_rate_value=getattr(priced.rate, "rate", None),
                    commission_rate=rate, commission=commission, trainer_share=share,
                    trainer_amount=Decimal("0.00"), trainer_currency=buyer_currency,
                    idempotency_key=idempotency_key, paid_with=PaidWith.WALLET)
        except IntegrityError as exc:
            # The same key, spent concurrently on another product and committed first
            # (the same key on THIS product waits on the product lock and replays).
            raise IdempotencyKeyReused() from exc

        _write_copy(product, buyer, purchase)

        escrow = get_escrow_wallet(buyer_currency)
        platform = get_platform_revenue_wallet(buyer_currency)
        trainer_wallet = _wallet_for(product.trainer, "trainer")
        lock_wallets(buyer.pk, [buyer_wallet, escrow, trainer_wallet, platform])
        common = {"product_id": product.pk, "purchase_id": purchase.pk,
                  "trainer_id": product.trainer_id}
        charge = move_funds_atomic(
            buyer_wallet, escrow, priced.amount, actor_id=buyer.pk, currency=buyer_currency,
            tx_type="transfer", money_type=MoneyType.ROUTINE_PRODUCT,
            metadata={"purpose": "routine_product_charge", **common})
        _pay_out_of_escrow(purchase, charge, escrow, trainer_wallet, platform,
                           currency=buyer_currency, share=share, commission=commission,
                           rate=rate, common=common)
        _notify_on_commit("sold", product, purchase=purchase)
    return purchase, True


def _pay_out_of_escrow(purchase, charge, escrow, trainer_wallet, platform, *, currency,
                       share, commission, rate, common, at=None) -> None:
    """The split of a sale whose money is in escrow — a wallet sale's (`buy`) and a
    checkout's (`grant_checkout`) alike: escrow -> trainer the share (an exchange when
    the trainer's currency is not `currency`, at the operator's rate as of `at`), escrow
    -> platform revenue the commission. Writes the three references and what reached
    the trainer onto `purchase`. The caller holds every wallet (`lock_wallets`)."""
    from wallet.models import exchange_funds_atomic, move_funds_atomic

    payout = fee = None
    trainer_amount, trainer_currency = Decimal("0.00"), trainer_wallet.currency
    # The legs after the charge carry NO actor: the ledger serves `actor` on every
    # row the trainer reads, and the buyer's id there named who bought — the one
    # thing a sale never tells the trainer (the review, 2026-09-24). The buyer's row
    # is already locked by the caller (`lock_wallets(buyer.pk, ...)`), which is all
    # `move_funds_atomic` used the actor for.
    if share > 0:
        if trainer_wallet.currency == currency:
            payout = move_funds_atomic(
                escrow, trainer_wallet, share, actor_id=None, currency=currency,
                tx_type="transfer", money_type=MoneyType.ROUTINE_PRODUCT,
                metadata={"purpose": "routine_product_sale", **common})
        else:
            # The escrow side is the given one, as in `users.coaching.pay_out`:
            # exactly `share` leaves escrow and the trainer's figure is derived.
            _debit, payout = exchange_funds_atomic(
                escrow, trainer_wallet, share, amount_in=currency,
                actor_id=None, money_type=MoneyType.ROUTINE_PRODUCT,
                metadata={"purpose": "routine_product_sale", **common}, at=at)
        trainer_amount = payout.amount
    if commission > 0:
        fee = move_funds_atomic(
            escrow, platform, commission, actor_id=None, currency=currency,
            tx_type="transfer", money_type=MoneyType.ROUTINE_PRODUCT,
            metadata={"purpose": "routine_product_commission", "rate": str(rate),
                      **common})
    purchase.trainer_amount, purchase.trainer_currency = trainer_amount, trainer_currency
    purchase.charge_reference = charge.reference_id
    purchase.payout_reference = payout.reference_id if payout else ""
    purchase.commission_reference = fee.reference_id if fee else ""
    purchase.save(update_fields=["trainer_amount", "trainer_currency", "charge_reference",
                                 "payout_reference", "commission_reference", "updated_at"])


# ---------------------------------------------------------------- the checkout (hub)
def lock_for_checkout(product_id) -> RoutineProduct:
    """The product row, locked for a checkout to open or be delivered — the lock a
    wallet sale takes first. `ProductNotForSale` when there is no such product."""
    product = (RoutineProduct.objects.select_for_update(of=("self",))
               .select_related("source_template", "trainer").filter(pk=product_id).first())
    if product is None:
        raise ProductNotForSale()
    return product


def checkout_terms(product, buyer):
    """What a checkout of `product` charges `buyer`, decided now (the caller holds the
    product lock): `(priced, snapshot)`.

    Refused as a wallet sale is — `AlreadyOwned`, `ProductNotForSale`, and
    `ExchangeRateUnavailable` when the price has to cross currencies and the rate is
    missing or stale. `priced` is `subscription.pricing.charge_for` in the buyer's own
    currency; `snapshot` is what the checkout keeps of the product, with the COMMISSION
    RATE its sale will be split at, snapshotted when the price is locked (the spec,
    section 8), so a later change of the rate never touches a checkout in flight.
    """
    from subscription.pricing import charge_for
    from users.coaching import commission_rate
    from wallet.models import currency_for

    owned = owned_purchase(buyer, product)
    if owned is not None:
        raise AlreadyOwned(purchase_id=owned.pk,
                           routine_id=getattr(getattr(owned, "routine", None), "pk", None))
    if not product.is_on_sale:
        raise ProductNotForSale()
    priced = charge_for(product.price, product.currency, currency_for(buyer))
    snapshot = {"id": product.pk, "trainer_id": product.trainer_id,
                "title_ar": product.title_ar, "title_en": product.title_en,
                "price": str(product.price), "currency": product.currency,
                "commission_rate": str(commission_rate())}
    return priced, snapshot


def checkout_refusal(payment) -> str | None:
    """Why a checkout's program cannot be delivered now that its money has arrived — a
    review reason (`subscription.TransferReview.REASONS`) — or None when it can.

    * `already_owned`: the buyer owns a confirmed copy (bought another way while the
      checkout was open, or through an older checkout whose money came late);
    * `product_unavailable`: the program cannot be written (its template is gone) or the
      trainer's share cannot be paid in their currency at the rate of the moment the
      price was locked.

    A product that merely went off sale after the checkout opened IS delivered: the
    buyer was offered it at that price and paid it. Locks the product row; the caller
    holds the payment's (payment, then product).
    """
    from users.trainer_relations import _wallet_for
    from wallet.exchange import current_rate, is_stale

    product = (RoutineProduct.objects.select_for_update(of=("self",))
               .select_related("source_template", "trainer")
               .filter(pk=payment.routine_product_id).first())
    if product is None or product.source_template_id is None:
        return "product_unavailable"
    if owned_purchase(payment.buyer, product) is not None:
        return "already_owned"
    trainer_currency = _wallet_for(product.trainer, "trainer").currency
    # The exchange of the share (`_pay_out_of_escrow`, `at=payment.created_at`) refuses
    # a missing or stale rate for that moment; asked here, without converting anything
    # (the conversion has one boundary, `wallet/exchange.py`), so the money is held
    # rather than the completion failing.
    if trainer_currency != payment.currency and is_stale(
            current_rate(at=payment.created_at), at=payment.created_at):
        return "product_unavailable"
    return None


def grant_checkout(payment) -> RoutinePurchase:
    """Deliver a checkout `PaymentService.complete_payment` has just confirmed: the
    purchase, the buyer's copy, and the sale's ledger rows (the module docstring).

    Inside the completion's transaction, with the payment locked; the caller has asked
    `checkout_refusal` first. The split is taken from what ARRIVED (`payment.amount`,
    which an operator's approval of a near miss sets to the transfer) at the commission
    rate the checkout snapshotted. Every row is ROUTINE_PRODUCT; the purchase is
    `paid_with="shamcash"` and names the checkout's reference, which is unique — one
    checkout delivers one purchase, once.
    """
    from users.coaching import lock_wallets, split
    from users.trainer_relations import _wallet_for
    from wallet.models import move_funds_atomic
    from wallet.utils import get_escrow_wallet, get_platform_revenue_wallet

    product = lock_for_checkout(payment.routine_product_id)
    buyer = payment.buyer
    snapshot = (payment.metadata or {}).get("product") or {}
    rate = Decimal(str(snapshot["commission_rate"]))
    amount, currency = Decimal(payment.amount), payment.currency
    commission, share = split(amount, rate)
    purchase = RoutinePurchase.objects.create(
        buyer=buyer, product=product, status=PurchaseStatus.CONFIRMED,
        price_amount=(payment.price_amount if payment.price_amount is not None else amount),
        price_currency=payment.price_currency or currency,
        amount=amount, currency=currency,
        exchange_rate=payment.exchange_rate, exchange_rate_value=payment.exchange_rate_value,
        commission_rate=rate, commission=commission, trainer_share=share,
        trainer_amount=Decimal("0.00"), trainer_currency=currency,
        idempotency_key=f"{CHECKOUT_KEY_PREFIX}{payment.pk}", paid_with=PaidWith.SHAMCASH,
        checkout_reference=payment.gateway_transaction_reference)
    _write_copy(product, buyer, purchase)

    escrow = get_escrow_wallet(currency)
    platform = get_platform_revenue_wallet(currency)
    trainer_wallet = _wallet_for(product.trainer, "trainer")
    lock_wallets(buyer.pk, [escrow, trainer_wallet, platform])
    common = {"product_id": product.pk, "purchase_id": purchase.pk,
              "trainer_id": product.trainer_id, "payment_id": str(payment.pk)}
    # The money enters the ledger in escrow: it came from ShamCash, not from the
    # buyer's wallet. `original_payment_id` marks it, as a plan payment's mint is marked,
    # so nothing reads it as a cash top-up.
    money_in = move_funds_atomic(
        None, escrow, amount, actor_id=buyer.pk, currency=currency, tx_type="topup",
        money_type=MoneyType.ROUTINE_PRODUCT,
        metadata={"purpose": "routine_product_checkout",
                  "original_payment_id": str(payment.pk), **common})
    _pay_out_of_escrow(purchase, money_in, escrow, trainer_wallet, platform,
                       currency=currency, share=share, commission=commission, rate=rate,
                       common=common, at=payment.created_at)
    _notify_on_commit("sold", product, purchase=purchase)
    return purchase


def refund_checkout(payment, actor, reason) -> RoutinePurchase:
    """The sale half of refunding a checkout's payment (`PaymentService.refund_payment`
    calls it, with the payment locked and already moved to `refunded`): reverse the
    purchase it delivered and revoke the copy. `ReviewConflict` — the caller rolls the
    whole refund back — when a wallet no longer holds its part."""
    with transaction.atomic():
        purchase = (RoutinePurchase.objects.select_for_update(of=("self",))
                    .select_related("product")
                    .filter(checkout_reference=payment.gateway_transaction_reference)
                    .first())
        if purchase is None:
            raise ReviewConflict("No purchase was delivered for this checkout.")
        return _reverse_sale(purchase, actor, (reason or "").strip() or "Refunded")


# ---------------------------------------------------------------- the refund
def refund(purchase_id, operator, reason) -> RoutinePurchase:
    """Give the buyer their money back and revoke their copy (see the module docstring).

    Raises `ReviewConflict` — nothing moved — when the purchase is not confirmed, a leg
    was already reversed by hand, or the trainer's wallet no longer holds their share.

    A purchase paid through a checkout (`paid_with="shamcash"`) is refunded through its
    PAYMENT (`PaymentService.refund_payment`), so the payment and the sale are one fact
    whichever door the operator used, and the lock order is the payment's (payment,
    then purchase).
    """
    reason = (reason or "").strip()
    if not reason:
        raise ReviewConflict("Say why the purchase is refunded: it is kept with the record.")
    if len(reason) > MAX_REASON:
        raise ReviewConflict(f"The reason is too long: at most {MAX_REASON} characters.")
    paid = (RoutinePurchase.objects.filter(pk=purchase_id)
            .values("paid_with", "checkout_reference", "status").first())
    if paid is None:
        raise RoutinePurchase.DoesNotExist(f"Purchase {purchase_id} does not exist")
    if paid["paid_with"] == PaidWith.SHAMCASH:
        return _refund_through_the_checkout(purchase_id, paid, operator, reason)
    with transaction.atomic():
        purchase = (RoutinePurchase.objects.select_for_update(of=("self",))
                    .select_related("product").get(pk=purchase_id))
        return _reverse_sale(purchase, operator, reason)


def _refund_through_the_checkout(purchase_id, paid, operator, reason) -> RoutinePurchase:
    from subscription.models import Payment
    from subscription.services.payment_service import By, PaymentError, PaymentService

    if paid["status"] != PurchaseStatus.CONFIRMED:
        raise ReviewConflict(f"This purchase is {paid['status']}: it was refunded already.")
    payment_id = (Payment.objects.filter(gateway_transaction_reference=paid["checkout_reference"])
                  .values_list("pk", flat=True).first())
    if payment_id is None:
        raise ReviewConflict("The checkout this purchase was paid through is missing.")
    try:
        PaymentService.refund_payment(payment_id, reason, by=By.OPERATOR, actor=operator)
    except PaymentError as exc:
        raise ReviewConflict(str(exc)) from exc
    return RoutinePurchase.objects.get(pk=purchase_id)


def _reverse_sale(purchase, actor, reason) -> RoutinePurchase:
    """Reverse every leg of a sale, revoke the copy and mark the purchase refunded. The
    caller holds the purchase lock (and, for a checkout's sale, the payment's first).
    A checkout's first leg brought the money into escrow from outside, so its reversal
    takes it out again: handed back by hand, as a plan refund's."""
    from users.coaching import lock_wallets
    from wallet.models import AlreadyReversed, InsufficientBalance, Transaction, move_funds_atomic

    from .models import Routine

    actor_id = getattr(actor, "pk", None)
    if purchase.status != PurchaseStatus.CONFIRMED:
        raise ReviewConflict(f"This purchase is {purchase.status}: it was refunded already.")
    routine = (Routine.objects.select_for_update(of=("self",))
               .filter(purchase=purchase).first())
    if routine is not None:
        # Revoked before the wallets are locked (routine rows, then user rows, then
        # wallets); a refusal below rolls it back with everything else.
        routine.assigned_to.remove(purchase.buyer_id)
        routine.is_active = False
        routine.save(update_fields=["is_active", "updated_at"])
    refs = [r for r in (purchase.charge_reference, purchase.payout_reference,
                        purchase.commission_reference) if r]
    legs = {tx.reference_id: tx for tx in Transaction.objects.filter(reference_id__in=refs)
            .select_related("source_wallet", "destination_wallet")}
    charge = legs.get(purchase.charge_reference)
    payout = legs.get(purchase.payout_reference)
    fee = legs.get(purchase.commission_reference)
    if charge is None:
        raise ReviewConflict("The ledger row of this purchase's charge is missing.")
    debit = None
    if payout is not None and payout.exchange_reference:
        debit = (Transaction.objects.select_related("source_wallet")
                 .get(reference_id=payout.exchange_reference))
    escrow, buyer_wallet = charge.destination_wallet, charge.source_wallet
    wallets = [w for w in (buyer_wallet, escrow,
                           payout.destination_wallet if payout else None,
                           fee.destination_wallet if fee else None) if w is not None]
    lock_wallets(actor_id, wallets)
    common = {"product_id": purchase.product_id, "purchase_id": purchase.pk,
              "trainer_id": purchase.product.trainer_id, "reason": reason}
    kind = MoneyType.ROUTINE_PRODUCT
    try:
        if payout is not None and debit is not None:
            # An exchange is undone as the exchange it was, at its own rate: the
            # trainer gives back exactly what they received, escrow gets back exactly
            # what left it.
            move_funds_atomic(payout.destination_wallet, None, payout.amount,
                              actor_id=actor_id, tx_type="reversal", reverses=payout,
                              currency=payout.currency, money_type=kind,
                              metadata={"original_reference": payout.reference_id, **common})
            move_funds_atomic(None, debit.source_wallet, debit.amount,
                              actor_id=actor_id, tx_type="reversal", reverses=debit,
                              currency=debit.currency, money_type=kind,
                              metadata={"original_reference": debit.reference_id, **common})
        elif payout is not None:
            move_funds_atomic(payout.destination_wallet, escrow, payout.amount,
                              actor_id=actor_id, tx_type="reversal", reverses=payout,
                              currency=payout.currency, money_type=kind,
                              metadata={"original_reference": payout.reference_id, **common})
        if fee is not None:
            move_funds_atomic(fee.destination_wallet, escrow, fee.amount,
                              actor_id=actor_id, tx_type="reversal", reverses=fee,
                              currency=fee.currency, money_type=kind,
                              metadata={"original_reference": fee.reference_id, **common})
        move_funds_atomic(escrow, buyer_wallet, charge.amount, actor_id=actor_id,
                          tx_type="reversal", reverses=charge, currency=charge.currency,
                          money_type=kind,
                          metadata={"original_reference": charge.reference_id, **common})
    except InsufficientBalance as exc:
        raise ReviewConflict("A wallet this sale paid into — the trainer's, as a rule — "
                             "no longer holds its part, so it cannot be taken back. "
                             "Nothing was moved.") from exc
    except AlreadyReversed as exc:
        raise ReviewConflict(f"Ledger row {exc.reference_id} of this purchase was already "
                             f"reversed by hand. Nothing was moved.") from exc
    purchase.status = PurchaseStatus.REFUNDED
    purchase.refunded_at = timezone.now()
    purchase.refunded_by = actor if actor_id is not None else None
    purchase.refund_reason = reason
    purchase.save(update_fields=["status", "refunded_at", "refunded_by", "refund_reason",
                                 "updated_at"])
    _notify_on_commit("refunded", purchase.product, purchase=purchase)
    return purchase


# ---------------------------------------------------------------- the pushes
def _notify_on_commit(what, product, *, purchase=None, reason="") -> None:
    """The pushes and inbox rows, once the change is committed. `robust=True`: the sale,
    the decision or the refund stands whatever happens to a notification."""
    from notifications.domain.dispatcher import emit_event
    from notifications.domain import product_events as events

    common = {"product_id": product.pk}
    if what == "approved":
        emitted = [events.RoutineProductApprovedEvent(recipient_id=product.trainer_id, **common)]
    elif what == "rejected":
        emitted = [events.RoutineProductRejectedEvent(recipient_id=product.trainer_id,
                                                      reason=reason, **common)]
    elif what == "sold":
        emitted = [events.RoutineProductSoldEvent(recipient_id=product.trainer_id,
                                                  purchase_id=purchase.pk, **common),
                   events.RoutineProductPurchasedEvent(recipient_id=purchase.buyer_id,
                                                       purchase_id=purchase.pk, **common)]
    else:
        emitted = [events.RoutineProductRefundedEvent(recipient_id=purchase.buyer_id,
                                                      purchase_id=purchase.pk, **common),
                   events.RoutineProductSaleRefundedEvent(recipient_id=product.trainer_id,
                                                          purchase_id=purchase.pk, **common)]
    for event in emitted:
        transaction.on_commit(lambda event=event: emit_event(event), robust=True)


__all__ = [
    "AlreadyOwned", "CheckoutOpen", "IdempotencyKeyReused", "MAX_PREVIEW_DAYS",
    "MAX_PROGRAM_DAYS", "PreviewCoversProgram", "ProductAlreadyRetired", "ProductIncomplete",
    "ProductNotEditable", "ProductNotForSale", "ProductNotSubmittable", "ProductRefusal",
    "ProgramTooLong", "ReviewConflict", "TrainerNotVerified", "approve", "buy",
    "catalogue_for", "checkout_refusal", "checkout_terms", "grant_checkout",
    "lock_for_checkout", "net_of", "open_checkout_for", "owned_purchase", "preview_of",
    "program_days", "refund", "refund_checkout", "reject", "retire", "submit",
    "visible_product",
]

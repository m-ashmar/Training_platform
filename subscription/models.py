from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.db.models import Q
from django.core.validators import MinValueValidator, MaxValueValidator
from django.core.exceptions import ValidationError
from users.models import CustomUser
from datetime import timedelta
import uuid
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from decimal import Decimal
from training_platform.encrypted_fields import EncryptedTextField
from training_platform.model_validation import RowValidationMixin
from policy.money import EntitlementSource, MoneyType
from wallet.currencies import SUPPORTED_CURRENCIES as _SUPPORTED_CURRENCIES


# Payment lifecycle state machine. The ONLY code allowed to move a payment
# between states is subscription.services.payment_service.PaymentService; any
# other write must respect these transitions (illegal ones raise).
PAYMENT_STATUS_TRANSITIONS = {
    'pending':    {'processing', 'authorized', 'completed', 'failed', 'cancelled', 'expired',
                   'needs_review'},
    'processing': {'authorized', 'completed', 'failed', 'expired', 'needs_review'},
    'authorized': {'completed', 'failed', 'expired', 'needs_review'},
    'completed':  {'refunded'},
    'failed':     {'pending'},   # allow a fresh retry
    'cancelled':  set(),
    'refunded':   set(),
    # Money that turns up for a checkout we had written off is still money. The poller
    # expires a checkout nobody finished so the customer can open a new one; if the
    # gateway then confirms the old one, refusing it would mean a customer who paid and
    # got nothing, and a webhook retrying for ever against a row that can never move.
    'expired':    {'completed', 'needs_review'},
    # Money arrived that is almost right — the reference with another amount, or the
    # amount with no reference (the spec, section 7; BACKEND_BUGS B-051). It is held
    # for an operator, never dropped: they approve it (completed) or reject it. An
    # exact transfer that turns up meanwhile still completes it.
    'needs_review': {'completed', 'rejected'},
    'rejected':   set(),
}

#: A payment that can still turn into money. A subscription has at most ONE of these at
#: a time: it is the checkout, whichever button opened it. See PaymentService.start_payment.
#: `needs_review` is one of them: money was seen for it, so it blocks a second checkout
#: and keeps the buyer's currency locked like any other; unlike the others it never
#: expires (`subscription.tasks.poll_open_payments`, `utils.checkout_expires_at`).
OPEN_PAYMENT_STATUSES = ('pending', 'processing', 'authorized', 'needs_review')

#: Open, but held for an operator: never expired, no countdown.
HELD_PAYMENT_STATUSES = ('needs_review',)

# Currencies the platform prices and charges in. BOTH are sold (the owner, 2026-09-21):
# Syrian pounds and US dollars, and nothing else, with no FX between them ever. The list
# lives in `wallet.currencies` because the ledger has to agree with the catalogue about
# it down to the letter — a plan priced in one and a wallet labelled the other is the
# shape of every currency bug this codebase has had. Re-exported under the name the
# migrations already deconstruct.
SUPPORTED_CURRENCIES = _SUPPORTED_CURRENCIES


class SubscriptionPlan(RowValidationMixin, models.Model):
    """Subscription plans with different features and pricing"""
    PLAN_TYPES = [
        ('basic', 'Basic'),
        ('premium', 'Premium'),
        ('pro', 'Professional'),
        ('enterprise', 'Enterprise'),
    ]
    
    name = models.CharField(max_length=100, unique=True, db_index=True)
    plan_type = models.CharField(max_length=20, choices=PLAN_TYPES, default='basic', db_index=True)
    description = models.TextField()
    price = models.DecimalField(
        max_digits=10, 
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0"), _("Price cannot be negative"))]
    )
    # A price without a currency is not a price. Every Payment inherits this value;
    # no view, serializer or gateway may substitute one of its own. Plans were priced
    # in SYP while three call sites stamped their payments 'USD', so the ledger was
    # denominated in a currency the platform never charged in and renewals could never
    # clear the gateway's currency check.
    currency = models.CharField(
        max_length=3,
        choices=SUPPORTED_CURRENCIES,
        default='SYP',
        db_index=True,
    )
    duration_days = models.IntegerField(
        default=30,
        validators=[MinValueValidator(1, _("Duration must be at least 1 day"))]
    )
    is_active = models.BooleanField(default=True, db_index=True)
    #: The plan the Plans screen badges "الأكثر اختياراً" (most chosen). An operator
    #: sets it; the app must not hard-code which plan is highlighted, because the
    #: catalogue is data (`screens/07-money/README.md`: "Plans and features come from
    #: the API, never hard-coded"). More than one may carry it — the app decides what
    #: to do with that; nothing here depends on it being unique.
    is_featured = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Feature flags
    has_diet_access = models.BooleanField(default=False, db_index=True)
    has_routine_access = models.BooleanField(default=False, db_index=True)
    has_challenges_access = models.BooleanField(default=False, db_index=True)
    has_ai_advice = models.BooleanField(default=False, db_index=True)
    has_priority_support = models.BooleanField(default=False, db_index=True)
    max_meals_per_day = models.IntegerField(
        default=3,
        validators=[MinValueValidator(0, _("Max meals cannot be negative"))]
    )
    max_routines = models.IntegerField(
        default=5,
        validators=[MinValueValidator(0, _("Max routines cannot be negative"))]
    )
    
    class Meta:
        # `id` tiebreaker: a single non-unique sort column is not a total order, so LIMIT/OFFSET
        # paging repeated and hid rows whenever two records shared the value.
        ordering = ['price', 'id']
        indexes = [
            models.Index(fields=['is_active', 'plan_type']),
            models.Index(fields=['price', 'is_active']),
        ]
        verbose_name = "Subscription Plan"
        verbose_name_plural = "Subscription Plans"
    
    def clean(self):
        """Custom validation"""
        if self.price < 0:
            raise ValidationError(_("Price cannot be negative"))
        if self.duration_days <= 0:
            raise ValidationError(_("Duration must be greater than 0"))
    
    def save(self, *args, **kwargs):
        self.validate_row()
        super().save(*args, **kwargs)
    
    def __str__(self):
        return f"{self.name} - {self.price} {self.currency}"

class Subscription(RowValidationMixin, models.Model):
    """User subscription details"""
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('expired', 'Expired'),
        ('cancelled', 'Cancelled'),
        ('pending', 'Pending'),
        ('trial', 'Trial'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(
        CustomUser,
        # PROTECT, not CASCADE: deleting a user cascaded to the subscription and from
        # there to every Payment, erasing the whole payment history (proven: 1 -> 0).
        # Retire users by deactivating them, never by hard delete.
        on_delete=models.PROTECT,
        related_name='subscription',
        db_index=True
    )
    plan = models.ForeignKey(
        SubscriptionPlan, 
        on_delete=models.PROTECT, 
        related_name='subscriptions',
        db_index=True
    )
    status = models.CharField(
        max_length=20, 
        choices=STATUS_CHOICES, 
        default='pending',
        db_index=True
    )
    
    start_date = models.DateTimeField(auto_now_add=True, db_index=True)
    end_date = models.DateTimeField(db_index=True)
    trial_end_date = models.DateTimeField(null=True, blank=True, db_index=True)
    
    auto_renew = models.BooleanField(default=True, db_index=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    # Where the money behind the current entitlement came from. Blank on a row that has
    # never entitled anyone (a checkout opened and not yet paid). Access NEVER reads it:
    # `is_active` is the only test, so what was bought on one channel works on all.
    source = models.CharField(max_length=16, choices=EntitlementSource.choices,
                              blank=True, default='', db_index=True)
    
    # Access flags: copied from the plan when the row is created or moved to another
    # plan, and an operator's override survives every other save (`save()` below).
    has_diet_access = models.BooleanField(default=False, db_index=True)
    has_routine_access = models.BooleanField(default=False, db_index=True)
    has_challenges_access = models.BooleanField(default=False, db_index=True)
    has_ai_advice = models.BooleanField(default=False, db_index=True)
    has_priority_support = models.BooleanField(default=False, db_index=True)
    
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        # `id` tiebreaker: a single non-unique sort column is not a total order, so LIMIT/OFFSET
        # paging repeated and hid rows whenever two records shared the value.
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(fields=['status', 'end_date']),
            models.Index(fields=['user', 'status']),
            models.Index(fields=['trial_end_date', 'status']),
            models.Index(fields=['auto_renew', 'status']),
            # Matches the real access pattern: WHERE <owner>=? ORDER BY created_at DESC, id DESC.
            # A single-column created_at index cannot serve that; this one can.
            models.Index(fields=['user', '-created_at', '-id'], name='subscription_owner_recent_idx'),
        ]
        verbose_name = "Subscription"
        verbose_name_plural = "Subscriptions"
    
    def clean(self):
        """Custom validation"""
        if self.end_date and self.start_date and self.end_date <= self.start_date:
            raise ValidationError(_("End date must be after start date"))
        if self.trial_end_date and self.start_date and self.trial_end_date <= self.start_date:
            raise ValidationError(_("Trial end date must be after start date"))
    
    def save(self, *args, **kwargs):
        from django.utils import timezone
        
        # Set start_date if not provided (for new objects)
        if not self.start_date:
            self.start_date = timezone.now()
        
        # Only auto-derive end_date if missing; allow admin edits
        if not self.end_date and self.plan:
            self.end_date = self.start_date + timedelta(days=self.plan.duration_days)
        
        # Inherit the plan's features when the row is CREATED and when its PLAN CHANGES —
        # never on any other save. This copied all five on every save, so an operator's
        # per-subscriber grant (the fields say "can be overridden") was reverted by the
        # very next save of the row: an activation, a renewal, a cancel (SIDE_BUGS S-049).
        # A change to the plan's own flags reaches a subscriber when they are moved onto
        # a plan, not as a side effect of whatever next happens to save their row.
        if self.plan_id and (self._state.adding
                             or self.plan_id != getattr(self, '_loaded_plan_id', None)):
            self.inherit_plan_features()

        self.validate_row()
        super().save(*args, **kwargs)
        self._loaded_plan_id = self.plan_id

    #: The five access flags a subscription copies from its plan.
    PLAN_FEATURE_FLAGS = ('has_diet_access', 'has_routine_access', 'has_challenges_access',
                          'has_ai_advice', 'has_priority_support')

    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        # The plan this row was READ with, so `save()` can tell a plan change from any
        # other write. Absent when `plan` was deferred, which reads as "changed" and
        # re-inherits — the behaviour before overrides were kept.
        instance._loaded_plan_id = instance.__dict__.get('plan_id')
        return instance

    def refresh_from_db(self, *args, **kwargs):
        super().refresh_from_db(*args, **kwargs)
        self._loaded_plan_id = self.plan_id

    def inherit_plan_features(self):
        """Copy the plan's five access flags onto this row, discarding any override."""
        for name in self.PLAN_FEATURE_FLAGS:
            setattr(self, name, getattr(self.plan, name))

    #: Statuses that entitle the holder to the plan's features.
    ENTITLED_STATUSES = ('active', 'trial')

    @property
    def is_active(self):
        """Whether this subscription entitles its holder to the plan's features right now.

        A trial did not. Trials are created with `status='trial'` and this required
        `'active'`, so every trial user was refused on all 25 diet endpoints and on
        routine and challenges — the whole point of the trial.
        """
        now = timezone.now()
        return (
            self.status in self.ENTITLED_STATUSES and
            self.end_date > now and
            (not self.trial_end_date or self.trial_end_date > now)
        )
    
    @property
    def effective_end(self):
        """When the entitlement ends: the trial's end if it comes first."""
        if self.trial_end_date and (self.end_date is None or self.trial_end_date < self.end_date):
            return self.trial_end_date
        return self.end_date

    @property
    def is_frozen(self):
        """Lapsed after once entitling its holder (the owner, 2026-09-19): the last diet
        plan stays READABLE and nothing can be used until the plan is paid again.

        Not active, and either in a status only an entitled row reaches (active or trial
        whose date passed before the hourly sweep, or expired), or with a `source`
        (paid, trial or granted, then cancelled or refunded). A checkout opened and
        never paid is `pending` with no source: never entitled, not frozen.
        """
        if self.is_active:
            return False
        return self.status in ('active', 'trial', 'expired') or bool(self.source)

    @property
    def access(self):
        """'active' | 'frozen' | 'inactive' — what the app shows (read-only after a lapse)."""
        if self.is_active:
            return 'active'
        return 'frozen' if self.is_frozen else 'inactive'

    def current_run_payments(self):
        """The completed payments that paid for THIS run of cover.

        A run starts when money clears with nothing running: activation moves
        `start_date` to that moment (`PaymentService._activate_subscription`), so a
        period that began before `start_date` belongs to cover that already ran out or
        was given up — an immediate cancel keeps its payment `completed`, with its old
        `covers_until` still in the future. The refund's own rule
        (`PaymentService._cover_to_take_back`) draws the same line. Nor does a payment
        an upgrade credited (`metadata['credited_into']`): its value moved into the
        upgrade's period.
        """
        payments = self.payments.filter(status='completed')
        if self.start_date:
            payments = payments.filter(covers_from__gte=self.start_date)
        # A payment whose remaining days an upgrade credited has been spent: its value
        # now lives in the upgrade's period (`PaymentService._apply_upgrade`). Left in,
        # a renewal stacked before an upgrade was credited AND still counted.
        return payments.exclude(metadata__has_key='credited_into')

    def last_run_plans(self):
        """Every plan the payments of the last run bought (`current_run_payments`).

        What a FROZEN row may still read follows what was paid for, not the plan the
        row is on now: `change-plan` moves an unpaid row onto another plan and its five
        flags, and choosing a plan without diet — buying nothing — took the frozen diet
        read away (`HasDietAccessOrFrozenRead`). All of the run's plans, not the most
        recent payment's: a late transfer for Basic that cleared inside a Premium run is
        the most recent payment, and Premium is still what that run was on.
        """
        ids = {p.plan_bought()['plan_id']
               for p in self.current_run_payments().select_related('subscription__plan')}
        if not ids:
            return []
        return list(SubscriptionPlan.objects.filter(pk__in=ids))

    @property
    def is_trial(self):
        """Check if subscription is in trial period"""
        now = timezone.now()
        return (
            self.status == 'trial' and 
            self.trial_end_date and 
            self.trial_end_date > now
        )
    
    @property
    def days_remaining(self):
        """Get days remaining in subscription"""
        now = timezone.now()
        if self.trial_end_date and self.trial_end_date > now:
            return (self.trial_end_date - now).days
        return max(0, (self.end_date - now).days)
    
    def __str__(self):
        return f"{self.user.username} - {self.plan.name} ({self.status})"

def plan_snapshot(plan) -> dict:
    """What a payment records about the plan it concerns (`Payment.plan_bought`).

    Written by every row that names a plan: a checkout when it is priced
    (`PaymentService._checkout_under_lock`) and a cancellation's audit row. The name is
    kept in every served language, so a receipt reprinted in the other language still
    names what was bought rather than what the plan is called today.
    """
    from django.conf import settings

    snap = {'id': plan.id, 'plan_type': plan.plan_type,
            'duration_days': plan.duration_days}
    for code, _label in settings.LANGUAGES:
        snap[f'name_{code}'] = getattr(plan, f'name_{code}', None) or ''
    return snap


def _snapshot_name(snap: dict) -> str:
    """The snapshot's plan name in the active language, falling back as the plan
    list itself does (`MODELTRANSLATION_FALLBACK_LANGUAGES`)."""
    from django.conf import settings
    from django.utils.translation import get_language

    active = (get_language() or settings.LANGUAGE_CODE).split('-')[0]
    order = [active, *getattr(settings, 'MODELTRANSLATION_FALLBACK_LANGUAGES', ())]
    for code in order:
        if snap.get(f'name_{code}'):
            return snap[f'name_{code}']
    return snap.get('name', '')


def paid_by(user) -> Q:
    """The payments `user` made: a plan's (through their subscription) or a routine
    product's checkout (`buyer`). The one filter for "the caller's own payment"."""
    return Q(subscription__user=user) | Q(buyer=user)


class Payment(RowValidationMixin, models.Model):
    """One checkout and the money that settles it: THE CHECKOUT SESSION of the payments
    spec, section 5 (the mapping is in `PaymentService`'s docstring).

    It pays for ONE of two things, and the database holds it to that
    (`payment_pays_for_one_thing`):

    * a PLAN — `subscription` is set, `buyer` and `routine_product` are not. Every row
      written before the web hub is one of these, and so is every row the app's own
      buttons open;
    * a ROUTINE PRODUCT bought through a checkout (the hub, `api/v1/checkout/sessions/`)
      — `buyer` and `routine_product` are set, `subscription` is not, and `money_type`
      is ROUTINE_PRODUCT. Its confirmation, through `PaymentService` like a plan's,
      writes the buyer's copy and the sale's ledger rows (`routine.products.
      grant_checkout`). A buyer holds at most one OPEN checkout per product
      (`uniq_open_product_checkout`).
    """
    PAYMENT_STATUS = [
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('authorized', 'Authorized'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
        ('refunded', 'Refunded'),
        ('cancelled', 'Cancelled'),
        ('expired', 'Expired'),
        ('needs_review', 'Needs review'),
        ('rejected', 'Rejected'),
    ]

    PAYMENT_METHOD = [
        # Active gateway
        ('shamcash', 'ShamCash'),
        # Manual / admin
        ('manual', 'Manual'),
        # Legacy (retained for historical rows; not selectable via gateway config)
        ('syriatel_cash', 'Syriatel Cash'),
        ('baraka_bank', 'Al-Baraka Bank'),
        ('bemo_bank', 'BEMO Bank'),
        ('stripe', 'Stripe'),
        ('paypal', 'PayPal'),
        ('apple_pay', 'Apple Pay'),
        ('google_pay', 'Google Pay'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # NULL only on a routine product's checkout (see the class docstring).
    subscription = models.ForeignKey(
        Subscription,
        on_delete=models.PROTECT,
        related_name='payments',
        db_index=True,
        null=True,
        blank=True,
    )
    #: A routine product's checkout: who is buying, and which program. Both NULL on a
    #: plan's payment. PROTECT, like the subscription: a record of money is kept.
    buyer = models.ForeignKey(CustomUser, null=True, blank=True, on_delete=models.PROTECT,
                              related_name='product_payments')
    routine_product = models.ForeignKey('routine.RoutineProduct', null=True, blank=True,
                                        on_delete=models.PROTECT, related_name='checkouts')
    amount = models.DecimalField(
        max_digits=10, 
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0"), _("Amount cannot be negative"))]
    )
    # THE CURRENCY THE BUYER WAS CHARGED IN, which is the buyer's own — their wallet's
    # (`wallet.currency_for`). The owner (2026-09-21): a person picks SYP or USD and
    # every charge they pay is in it, whatever the thing they are buying is priced in.
    # Never chosen by a caller and never by the client. `choices` is enforced because
    # Payment.save() runs validate_row().
    currency = models.CharField(
        max_length=3,
        choices=SUPPORTED_CURRENCIES,
        default='SYP',
        db_index=True,
    )

    # --- what the plan actually costs, and how that became `amount`/`currency` -------
    # THE PRICE OF RECORD: the number and the unit the operator priced the plan in,
    # copied here when the checkout opened. When the buyer trades in the same currency
    # these simply repeat `amount`/`currency`. When they do not, `amount` is this price
    # converted at `exchange_rate`, and these three are the only evidence of what was
    # sold and at what rate — which is what the receipt states and what a refund
    # reverses at. Read them, never today's rate: the pound moves, and re-valuing a
    # past purchase from the current row would hand a customer back a different number
    # than they paid.
    #
    # NULL on rows written before the platform sold in two currencies; for those the
    # price of record is `amount`/`currency`, because nothing was ever converted.
    price_amount = models.DecimalField(max_digits=10, decimal_places=2,
                                       null=True, blank=True)
    price_currency = models.CharField(max_length=3, choices=SUPPORTED_CURRENCIES,
                                      blank=True, default='')
    #: The append-only rate row this charge was priced at, or NULL when nothing was
    #: converted. PROTECT, so the row can never be edited or deleted out from under it.
    exchange_rate = models.ForeignKey('wallet.ExchangeRate', null=True, blank=True,
                                      on_delete=models.PROTECT,
                                      related_name='payments')
    #: The same number, copied, so a receipt and an export read it without a join.
    exchange_rate_value = models.DecimalField(max_digits=18, decimal_places=6,
                                              null=True, blank=True)
    status = models.CharField(
        max_length=20, 
        choices=PAYMENT_STATUS, 
        default='pending',
        db_index=True
    )
    payment_method = models.CharField(
        max_length=20,
        choices=PAYMENT_METHOD,
        default='shamcash',
        db_index=True
    )

    # Which of the four kinds of money this is (`policy.money`). A row in this table pays
    # for a plan, so it is PLATFORM_SUB by construction; the column exists so that every
    # payment, ledger row and checkout answers the same question the same way, and the
    # channel policy reads it rather than inferring it from the table.
    money_type = models.CharField(max_length=16, choices=MoneyType.choices,
                                  default=MoneyType.PLATFORM_SUB, db_index=True)

    # External payment provider details
    transaction_id = models.CharField(max_length=255, blank=True, null=True, db_index=True)
    payment_intent_id = models.CharField(max_length=255, blank=True, null=True, db_index=True)

    # Gateway-specific fields
    gateway_response = models.JSONField(default=dict, blank=True)
    gateway_error = models.TextField(blank=True, null=True)
    gateway_transaction_reference = models.CharField(max_length=255, blank=True, null=True, db_index=True)
    # Idempotency: unique per gateway webhook event; blocks replayed/duplicate callbacks.
    gateway_event_id = models.CharField(max_length=255, blank=True, null=True, db_index=True)
    # Invoice number generated exactly once, when the payment completes.
    invoice_number = models.CharField(max_length=64, blank=True, null=True, db_index=True)

    # When the money was confirmed — the DATE ON THE RECEIPT, and the only field that
    # answers it. Stamped once by PaymentService and never written again, so a later
    # refund (which does move `updated_at`) cannot change the date of a receipt already
    # issued. NULL on rows completed before this column existed; `subscription.receipts`
    # falls back to `updated_at` for those, which is the best evidence they carry.
    completed_at = models.DateTimeField(null=True, blank=True, db_index=True)

    # The access this payment bought, stamped when it completes. A refund takes back
    # exactly this much. It used to take back `plan.duration_days` whenever any other
    # completed payment existed, with no idea what the refunded one had added — so
    # refunding a double charge cancelled the month the customer had still paid for, and
    # for a new subscriber landed `end_date` on `start_date` and crashed the refund on
    # every retry. NULL on rows completed before the period was recorded.
    covers_from = models.DateTimeField(null=True, blank=True)
    covers_until = models.DateTimeField(null=True, blank=True)

    # When the trainer payout for this payment was resolved: paid, or found not owed
    # (no trainer, or a person's wallet that holds a currency other than the one this
    # payment was charged in). NULL on a completed
    # payment means the payout is still OWED. The payout runs on commit, after the
    # payment row already says 'completed', and every retry used to decide "done" from
    # that status — so one transient fault in the receiver (a deadlock, a dropped
    # connection) answered 500, the gateway's redelivery answered 200, and the trainer
    # was never paid. Written by `wallet.signals.handle_payment_completed`.
    payout_settled_at = models.DateTimeField(null=True, blank=True)

    # Metadata
    description = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        # `id` tiebreaker: a single non-unique sort column is not a total order, so LIMIT/OFFSET
        # paging repeated and hid rows whenever two records shared the value.
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(fields=['status', 'created_at']),
            models.Index(fields=['payment_method', 'status']),
            models.Index(fields=['transaction_id']),
            models.Index(fields=['payment_intent_id']),
            # The payout sweep reads exactly these rows. Partial, so it stays the size of
            # the backlog rather than the size of the ledger.
            models.Index(
                fields=['updated_at'], name='payment_payout_owed_idx',
                condition=Q(status='completed', payout_settled_at__isnull=True),
            ),
        ]
        constraints = [
            # Idempotency guards — each external identifier maps to at most one
            # payment (NULLs excluded so pending rows without ids are allowed).
            models.UniqueConstraint(
                fields=['gateway_event_id'],
                condition=Q(gateway_event_id__isnull=False),
                name='uniq_payment_gateway_event_id',
            ),
            models.UniqueConstraint(
                fields=['gateway_transaction_reference'],
                condition=Q(gateway_transaction_reference__isnull=False),
                name='uniq_payment_gateway_reference',
            ),
            models.UniqueConstraint(
                fields=['transaction_id'],
                condition=Q(transaction_id__isnull=False),
                name='uniq_payment_transaction_id',
            ),
            models.UniqueConstraint(
                fields=['invoice_number'],
                condition=Q(invoice_number__isnull=False),
                name='uniq_payment_invoice_number',
            ),
            models.CheckConstraint(condition=Q(money_type__in=MoneyType.values),
                                   name='payment_money_type_known'),
            # A plan's payment, or a routine product's checkout — never both, never
            # neither (the class docstring).
            models.CheckConstraint(
                condition=(Q(subscription__isnull=False, buyer__isnull=True,
                             routine_product__isnull=True)
                           | Q(subscription__isnull=True, buyer__isnull=False,
                               routine_product__isnull=False,
                               money_type=MoneyType.ROUTINE_PRODUCT)),
                name='payment_pays_for_one_thing'),
            # One open checkout per buyer per product (the spec, section 5: "one open
            # session per user per item"). `PaymentService.start_product_checkout`
            # decides it under the product's row lock; this is the backstop.
            models.UniqueConstraint(
                fields=['buyer', 'routine_product'],
                condition=Q(routine_product__isnull=False,
                            status__in=OPEN_PAYMENT_STATUSES),
                name='uniq_open_product_checkout'),
        ]
        verbose_name = "Payment"
        verbose_name_plural = "Payments"

    def clean(self):
        """Custom validation"""
        if self.amount < 0:
            raise ValidationError(_("Payment amount cannot be negative"))

    def save(self, *args, **kwargs):
        self.validate_row()
        super().save(*args, **kwargs)

    def can_transition_to(self, new_status: str) -> bool:
        """Whether moving from the current status to new_status is legal."""
        if new_status == self.status:
            return True  # idempotent no-op
        return new_status in PAYMENT_STATUS_TRANSITIONS.get(self.status, set())

    def plan_bought(self) -> dict:
        """The plan this payment was priced for: `plan_id`, `plan_name`, `plan_type`,
        `duration_days`.

        Read from the snapshot the checkout wrote (`metadata['plan']`, `plan_snapshot`),
        because the subscription can move to another plan after it
        (`subscriptions/{id}/change-plan/`, BACKEND_BUGS B-047): reading
        `subscription.plan` then would name the NEW plan on every earlier receipt and
        history row. `duration_days` is the period that was SOLD, which is the period
        completion grants (`PaymentService._activate_subscription`) even if the operator
        edits the plan while the transfer is in flight — a receipt and the cover it
        bought must agree. The name is the snapshot's in the reader's language: the plan
        name is translated (`subscription/translation.py`), and freezing the one the
        checkout request happened to be in put Arabic on an English receipt.

        Rows opened before the snapshot existed fall back to the subscription's plan —
        what every row said before, and right while plans could not change. A routine
        product's checkout bought no plan: every value is empty.
        """
        snap = (self.metadata or {}).get('plan')
        if isinstance(snap, dict) and snap.get('id') is not None:
            return {'plan_id': snap['id'], 'plan_name': _snapshot_name(snap),
                    'plan_type': snap.get('plan_type', ''),
                    'duration_days': snap.get('duration_days')}
        if self.subscription_id is None:
            return {'plan_id': None, 'plan_name': '', 'plan_type': '', 'duration_days': None}
        plan = self.subscription.plan
        return {'plan_id': plan.id, 'plan_name': plan.name,
                'plan_type': plan.plan_type, 'duration_days': plan.duration_days}

    def product_bought(self) -> dict | None:
        """The routine product a web-hub checkout bought: `product_id`, `product_title`
        (in the reader's language), or None on a plan's payment (BACKEND_BUGS B-167).

        The title is the product's as it reads now, else the one its checkout's snapshot
        kept (`metadata['product']`), the same fallback the session reads use
        (`checkout_views._item`)."""
        if self.routine_product_id is None:
            return None
        from django.utils.translation import get_language

        language = get_language() or ''
        product = self.routine_product
        snapshot = (self.metadata or {}).get('product') or {}
        if product is not None:
            title = product.title_in(language)
        elif language.startswith('ar'):
            title = snapshot.get('title_ar') or snapshot.get('title_en') or ''
        else:
            title = snapshot.get('title_en') or snapshot.get('title_ar') or ''
        return {'product_id': self.routine_product_id, 'product_title': title}

    @property
    def payer_id(self):
        """The person paying: the buyer of a product's checkout, else the subscriber."""
        if self.buyer_id is not None:
            return self.buyer_id
        return self.subscription.user_id if self.subscription_id is not None else None

    @property
    def payer(self):
        if self.buyer_id is not None:
            return self.buyer
        return self.subscription.user if self.subscription_id is not None else None

    def __str__(self):
        payer = self.payer
        return (f"{getattr(payer, 'username', '-')} - {self.amount} {self.currency} "
                f"({self.status})")

class TransferReview(models.Model):
    """A transfer on the merchant account that no checkout claimed exactly: the review
    queue (the spec, section 7; BACKEND_BUGS B-051).

    "Right amount with wrong or missing reference, or right reference with wrong
    amount, goes to NEEDS_REVIEW with the evidence attached, never silently dropped."
    One row per gateway transaction (`transaction_id` is unique, so the same transfer
    can be held, and therefore confirm, once). When it names a checkout, that payment
    is `needs_review` while the row is open. Every decision is an operator's, through
    `PaymentService` (`approve_review`, `reject_review`, `attach_review`,
    `dismiss_review`), and records who, when and why.
    """

    REASONS = [
        ('amount_differs', 'The reference, another amount'),
        ('currency_differs', 'The reference, another currency'),
        ('reference_missing', 'The amount of one open checkout, no reference'),
        ('several_candidates', 'The amount of several open checkouts, no reference'),
        ('no_candidate', 'No open checkout matches'),
        ('checkout_closed', 'The reference of a checkout no longer open'),
        ('duplicate_transfer', 'The reference of a checkout already paid'),
        # A routine product's checkout whose money arrived but whose program cannot be
        # delivered (`routine.products.checkout_refusal`): the buyer owns it already, or
        # it can no longer be written or paid out. Money to hand back.
        ('already_owned', 'A program the buyer already owns'),
        ('product_unavailable', 'A program that can no longer be delivered'),
    ]
    STATUSES = [
        ('open', 'Open'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
        ('dismissed', 'Dismissed'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    gateway = models.CharField(max_length=30, default='shamcash')
    transaction_id = models.CharField(max_length=255, unique=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3)
    #: What the sender wrote, and who they were, as the gateway reports them.
    note = models.CharField(max_length=500, blank=True, default='')
    sender = models.CharField(max_length=255, blank=True, default='')
    occurred_at = models.DateTimeField(null=True, blank=True)
    raw = models.JSONField(default=dict, blank=True)
    reason = models.CharField(max_length=30, choices=REASONS)
    #: The checkout it is held against, when one is named (then that payment is
    #: `needs_review` while this row is open).
    payment = models.ForeignKey(Payment, null=True, blank=True, on_delete=models.PROTECT,
                                related_name='transfer_reviews')
    #: Every checkout the amount fits, when there is more than one.
    candidate_payment_ids = models.JSONField(default=list, blank=True)
    #: The second signal (the spec, section 6): the checkouts — this row's `payment` and
    #: its candidates — whose customer saved the transfer's sender address on their
    #: profile (`PayerAddress`). Evidence for the operator, written when the row is
    #: queued; it never confirms anything, and the address is self-declared.
    sender_match_payment_ids = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=10, choices=STATUSES, default='open', db_index=True)
    resolved_by = models.ForeignKey(CustomUser, null=True, blank=True,
                                    on_delete=models.SET_NULL,
                                    related_name='resolved_transfer_reviews')
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolution_note = models.CharField(max_length=500, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-id']
        indexes = [models.Index(fields=['status', 'created_at'])]
        verbose_name = "Transfer review"
        verbose_name_plural = "Transfer reviews"

    def __str__(self):
        return f"{self.transaction_id} {self.amount} {self.currency} ({self.reason}, {self.status})"


class PayerAddress(models.Model):
    """The public ShamCash account address a customer pays FROM, saved on their profile
    (the spec, section 6: "Save the address, not the account").

    An identifier, like an IBAN — never a password, PIN, OTP or session, which nothing
    here or anywhere asks for. One per account; typed twice, editable and removable
    (`payer-address/`, `subscription/payer_address.py`). Encrypted at rest like a
    trainer's payout address (`payouts.PayoutMethod`); `address_digest`, a keyed hash of
    the normalised address, is what a transfer's sender is compared with, since a
    randomised ciphertext cannot be.

    What it is used for, and nothing else: the SECOND signal when a transfer reaches the
    merchant account with the right amount and a missing or mistyped reference. A match
    is evidence on the operator's review queue (`TransferReview.sender_match_payment_ids`).
    It picks the likely checkout among several only once the address is PROVEN
    (`verified_at`): anyone can type anyone's address, and an unproven one let a person
    who saved a stranger's address have that stranger's transfer held on their own
    checkout (SIDE_BUGS S-241). It never confirms a payment on its own: only the
    reference and the exact amount do, or an operator.
    """

    user = models.OneToOneField(CustomUser, on_delete=models.CASCADE,
                                related_name='payer_address')
    #: Normalised (no spaces or dashes, lower case). Encrypted.
    address = EncryptedTextField(default='')
    #: `subscription.payer_address.address_digest` of `address`. Not unique: two accounts may type
    #: the same address, and refusing the second would tell a stranger that somebody
    #: saved it. A digest held by more than one account points at nobody.
    address_digest = models.CharField(max_length=64, db_index=True)
    #: When a transfer FROM this address first confirmed one of this customer's own
    #: checkouts by its exact reference and amount (`PaymentService.complete_payment`,
    #: `payer_address.verify_sender`) — the proof that the customer pays from it. Null
    #: until then, and again whenever the address is changed. Only a proven address
    #: decides which checkout a reference-less transfer is held on (SIDE_BUGS S-241).
    verified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at', '-id']
        verbose_name = "Payer ShamCash address"
        verbose_name_plural = "Payer ShamCash addresses"

    def __str__(self):
        return f"payer address of user {self.user_id}"


class GatewayHealth(models.Model):
    """Whether the server can read a payment gateway's merchant account right now (the
    spec, section 6: "Every payment screen needs a 'ShamCash is not responding, your
    payment is safe, we will keep checking' state").

    One row per gateway, written by every read of the merchant account — the pollers,
    the sweep, a customer's "check my payment" — through `PaymentGatewayService`
    (`subscription/gateway_health.py`): a read that answered moves `last_success_at`
    and clears the streak; one that could not reach the gateway adds to
    `consecutive_failures`, and the first failure of a streak sets `failing_since`.
    `state()` turns that into `ok` or `degraded`, which the payment reads answer as
    `gateway_state`. Nothing decides on it: a checkout is never expired, failed or
    completed because of this row (an unanswered read closes nothing,
    `subscription.tasks.poll_open_payments`). Operators see it in the console.
    """

    gateway = models.CharField(max_length=30, unique=True)
    #: The last read of the merchant account that answered (in full or not).
    last_success_at = models.DateTimeField(null=True, blank=True)
    #: The last read that could not reach the gateway, and why (operators only).
    last_failure_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=500, blank=True, default='')
    #: Reads that failed since the last one that answered.
    consecutive_failures = models.PositiveIntegerField(default=0)
    #: When the current streak of failures began; null while the gateway answers.
    failing_since = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['gateway', 'id']
        verbose_name = "Gateway health"
        verbose_name_plural = "Gateway health"

    def __str__(self):
        return f"{self.gateway}: {self.consecutive_failures} failure(s)"


class ImmutablePaymentEvent(PermissionError):
    """A payment event was asked to change or go. It never does: write a new one."""


class PaymentEvent(models.Model):
    """One state transition of one payment, never edited (the spec, section 7: "every
    transition writes an immutable event row (who or what, when, from, to, evidence)").

    Written by `PaymentService` in the SAME transaction as the transition it records —
    the one place a payment's state moves (`PaymentService._move`) — so a transition
    that rolls back takes its event with it, and one that commits cannot lack it. A
    checkout's opening is the first event (`from_status` empty).

    `save()` and `delete()` refuse an existing row, and a database trigger refuses
    UPDATE and DELETE for every other path (`QuerySet.update()`, a shell, the next bulk
    job): migration 0016, the reasoning of `wallet/migrations/0021` and
    `payouts/migrations/0002`. The one UPDATE it allows is `actor` becoming NULL with
    nothing else changed, because the column is `on_delete=SET_NULL`.

    Payments older than this table carry ONE event for the state they were in when it
    was created, marked `backfill` (migration 0016): what happened before that was never
    recorded, and the row says so rather than inventing a history.
    """

    class By(models.TextChoices):
        #: The customer: they opened the checkout, or asked "check my payment"
        #: (`payments/{id}/reconcile/`). `actor` is them.
        CUSTOMER = 'customer', 'Customer'
        #: A signed gateway webhook delivery.
        GATEWAY = 'gateway', 'Gateway webhook'
        #: The server asking the gateway about a checkout: the 60-second poller and the
        #: late-money poller (`subscription.tasks`).
        POLLER = 'poller', 'Poller'
        #: The ten-minute sweep of transfers no checkout claimed.
        SWEEP = 'sweep', 'Unclaimed-transfer sweep'
        #: A person in the operator console. `actor` is them.
        OPERATOR = 'operator', 'Operator'
        #: Anything else in the server, and every backfilled row.
        SYSTEM = 'system', 'System'

    payment = models.ForeignKey(Payment, on_delete=models.PROTECT, related_name='events')
    #: Empty for the checkout's opening (and for a backfilled row: unknown).
    from_status = models.CharField(max_length=20, blank=True, default='')
    to_status = models.CharField(max_length=20, choices=Payment.PAYMENT_STATUS)
    #: The person, when a person did it (`actor_role` customer or operator). Null for a
    #: gateway, a poller, the system, or once that account is gone.
    actor = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name='+')
    actor_role = models.CharField(max_length=16, choices=By.choices)
    #: What the transition rested on: the gateway's transaction id, event id, reference,
    #: amount and currency; the review row and the operator's note; the build that opened
    #: the checkout; the reason a checkout was closed. Ids and figures, never the raw
    #: gateway payload (that is `Payment.gateway_response`). The operator's to read: the
    #: app is shown `from`, `to`, when and `by` only.
    evidence = models.JSONField(default=dict, blank=True, encoder=DjangoJSONEncoder)
    #: Written by migration 0016 for a payment older than this table: the state it was
    #: in then, at the best time its row could give (`evidence['at_from']` says which).
    backfill = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['created_at', 'id']
        indexes = [models.Index(fields=['payment', 'created_at', 'id'],
                                name='payment_event_timeline_idx')]
        verbose_name = "Payment event"
        verbose_name_plural = "Payment events"

    def __str__(self):
        return f"{self.payment_id}: {self.from_status or '-'} -> {self.to_status}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ImmutablePaymentEvent("a payment event is never edited; write a new one")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ImmutablePaymentEvent("a payment event is never deleted")

    def for_app(self) -> dict:
        """What the customer's app is shown: the step, when, and who or what took it —
        never the evidence (a transfer's details are its sender's, a note is the
        operator's) and never which operator."""
        return {'from_status': self.from_status or None, 'to_status': self.to_status,
                'at': self.created_at, 'by': self.actor_role, 'backfill': self.backfill}


class SubscriptionFeature(models.Model):
    """Individual features that can be enabled/disabled per subscription"""
    name = models.CharField(max_length=100, unique=True, db_index=True)
    description = models.TextField()
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    
    class Meta:
        ordering = ['name']
        verbose_name = "Subscription Feature"
        verbose_name_plural = "Subscription Features"
    
    def __str__(self):
        return self.name

class SubscriptionUsage(RowValidationMixin, models.Model):
    """Track usage of subscription features"""
    subscription = models.ForeignKey(
        Subscription, 
        on_delete=models.CASCADE, 
        related_name='usage',
        db_index=True
    )
    feature = models.ForeignKey(
        SubscriptionFeature, 
        on_delete=models.CASCADE,
        db_index=True
    )
    usage_count = models.IntegerField(
        default=0,
        validators=[MinValueValidator(0, _("Usage count cannot be negative"))]
    )
    limit = models.IntegerField(
        default=0,  # 0 means unlimited
        validators=[MinValueValidator(0, _("Limit cannot be negative"))]
    )
    period_start = models.DateTimeField(
        db_index=True,
        help_text=(
            "Start of the window this row counts. Computed by subscription.quota, not "
            "stamped on insert: it was auto_now_add, so every row got a distinct value "
            "and the unique key below could never be violated. Concurrent requests each "
            "inserted their own row and the lookup then raised MultipleObjectsReturned."
        ),
    )
    period_end = models.DateTimeField(db_index=True)
    
    class Meta:
        # Deterministic total order. Without it Postgres returns rows in whatever order it
        # likes and LIMIT/OFFSET paging silently repeats and hides rows between pages.
        ordering = ['-id']
        # Enforceable now that period_start is a computed boundary rather than an
        # insertion timestamp: two requests inside the same window collide, which is
        # exactly what makes get_or_create race-safe.
        unique_together = ['subscription', 'feature', 'period_start']
        indexes = [
            models.Index(fields=['subscription', 'feature']),
            models.Index(fields=['period_end']),
        ]
        verbose_name = "Subscription Usage"
        verbose_name_plural = "Subscription Usage"
    
    def clean(self):
        """Custom validation"""
        if self.usage_count < 0:
            raise ValidationError(_("Usage count cannot be negative"))
        if self.limit < 0:
            raise ValidationError(_("Limit cannot be negative"))
        if self.period_end and self.period_start and self.period_end <= self.period_start:
            raise ValidationError(_("Period end must be after period start"))

    def save(self, *args, **kwargs):
        self.validate_row()
        super().save(*args, **kwargs)

    @property
    def usage_percentage(self):
        """Get usage as percentage of limit"""
        if self.limit == 0:
            return 0
        return min(100, (self.usage_count / self.limit) * 100)
    
    def __str__(self):
        return f"{self.subscription.user.username} - {self.feature.name}: {self.usage_count}/{self.limit}"
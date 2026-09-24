"""Routine products: a program a trainer lists for anyone to buy (plan step 10; the
payments spec, section 8 "Routine products", and section 3).

    DRAFT --submit (a VERIFIED trainer)--> IN_REVIEW --approve (operator)--> PUBLISHED
      ^                                        |
      +--------reject (operator, reason)-------+
    any state but RETIRED --retire (trainer)--> RETIRED

`RoutineProduct` is the listing and `RoutinePurchase` is one sale of it. Neither is a
coaching row and neither is ever read by the coaching code: the spec's line that must
never blur (section 3) — a routine written for one client inside coaching is
COACHING_1TO1, a routine listed for anyone to buy is ROUTINE_PRODUCT — is kept by the
models themselves, by separate endpoints (`routine/product_views.py`) and by the
buyer's copy being the buyer's (`routine.Routine.purchase`).

Imported at the bottom of `routine/models.py`, so Django registers both in the
`routine` app and their migration lives there with the template they are written from.
"""
from decimal import Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q


class ProductStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    IN_REVIEW = "in_review", "In review"
    PUBLISHED = "published", "Published"
    RETIRED = "retired", "Retired"


class ReviewDecision(models.TextChoices):
    NONE = "", "Not reviewed"
    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"


class PurchaseStatus(models.TextChoices):
    CONFIRMED = "confirmed", "Confirmed"
    REFUNDED = "refunded", "Refunded"


class PaidWith(models.TextChoices):
    """How the buyer paid. `wallet` is the only rail today; `shamcash` is the seam for
    a hosted checkout (the spec's section 5 hub), reserved so a sale paid there is one
    adapter and a `checkout_reference`, not a new model."""
    WALLET = "wallet", "Wallet"
    SHAMCASH = "shamcash", "ShamCash checkout (reserved)"


#: Most free days a buyer may read before paying (the form's chips: 0, 1, 3, 7).
MAX_PREVIEW_DAYS = 7
#: The dearest a product may be, in either currency. A ceiling against a typo, not a
#: price policy: the design's ruler runs to 8,000 SYP.
MAX_PRICE = Decimal("10000000.00")


class RoutineProduct(models.Model):
    """A trainer's program, listed for sale. See the module docstring for the states.

    The program itself is the trainer's TEMPLATE (`source_template`): the buyer's copy
    is written from it at purchase (`routine/assignment.routine_from_template`). A
    template can be edited at any time, so the review records which version of it the
    operator saw (`template_version`, the template's `updated_at` at submission), and a
    product whose template has moved on since is not sold until it is reviewed again
    (`is_on_sale`). The price is the trainer's, in the trainer's currency (`currency`,
    set by the server, never by the trainer).
    """
    trainer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                related_name="routine_products")
    #: SET_NULL, not PROTECT: deleting a template behind a DRAFT or RETIRED product is
    #: the trainer's business (the draft then needs another template). Deleting one
    #: behind a product in review or on sale is refused by the template route itself
    #: (409 `template_listed_for_sale`).
    source_template = models.ForeignKey("routine.RoutineTemplate", null=True, blank=True,
                                        on_delete=models.SET_NULL, related_name="products")
    title_ar = models.CharField(max_length=120, blank=True, default="")
    title_en = models.CharField(max_length=120, blank=True, default="")
    description_ar = models.TextField(max_length=2000, blank=True, default="")
    description_en = models.TextField(max_length=2000, blank=True, default="")
    #: The price of record. NULL until the trainer names one; a product cannot be
    #: submitted without it.
    price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True,
                                validators=[MinValueValidator(Decimal("0.01")),
                                            MaxValueValidator(MAX_PRICE)])
    #: The trainer's currency (`wallet.currency_for`), written by the server on every
    #: save of a draft. A person trades in one currency (the owner, 2026-09-21).
    currency = models.CharField(max_length=8)
    #: How many days of the program a buyer may read before paying.
    preview_days = models.PositiveSmallIntegerField(
        default=0, validators=[MaxValueValidator(MAX_PREVIEW_DAYS)])
    #: The cover, from the trainer's own Media Kit (a `MediaAsset` whose `person` is
    #: this trainer, an image). Not a copy: replacing that picture replaces the cover.
    cover = models.ForeignKey("mediakit.MediaAsset", null=True, blank=True,
                              on_delete=models.SET_NULL, related_name="+")
    status = models.CharField(max_length=16, choices=ProductStatus.choices,
                              default=ProductStatus.DRAFT, db_index=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    #: The template's `updated_at` when this was submitted: what the review saw.
    template_version = models.DateTimeField(null=True, blank=True)
    review_decision = models.CharField(max_length=16, choices=ReviewDecision.choices,
                                       blank=True, default="")
    #: The operator's words on a rejection; the trainer is shown them.
    review_reason = models.TextField(max_length=1000, blank=True, default="")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name="+")
    published_at = models.DateTimeField(null=True, blank=True, db_index=True)
    retired_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = "Routine product"
        verbose_name_plural = "Routine products"
        indexes = [models.Index(fields=["trainer", "-created_at", "-id"],
                                name="routineproduct_trainer_idx")]

    def __str__(self):
        return self.title_en or self.title_ar or f"Product {self.pk}"

    def title_in(self, language) -> str:
        """The title in `language`, the other language when that one is empty."""
        if (language or "").startswith("ar"):
            return self.title_ar or self.title_en
        return self.title_en or self.title_ar

    def description_in(self, language) -> str:
        if (language or "").startswith("ar"):
            return self.description_ar or self.description_en
        return self.description_en or self.description_ar

    @property
    def template_changed(self) -> bool:
        """True when the template moved on after this was submitted for review."""
        template = self.source_template
        if template is None:
            return self.template_version is not None
        return self.template_version is not None and template.updated_at != self.template_version

    @property
    def is_on_sale(self) -> bool:
        """Published, still backed by the template version its review saw."""
        return (self.status == ProductStatus.PUBLISHED and self.source_template_id is not None
                and not self.template_changed)


class RoutinePurchase(models.Model):
    """One sale of a routine product: who bought it, what they paid, and where every
    unit of it went. A record of money, so it is kept for ever (the privacy registry,
    `on_erase="keep"`, like `users.CoachingPeriod`).

    Everything is a SNAPSHOT of the moment of sale (the spec, section 8: "snapshotted
    onto the transaction at the moment the price is locked"): the price of record, what
    the buyer was charged in their own currency and at which rate, the commission rate
    and the split, and what reached the trainer in theirs. A later price, rate or
    commission change never touches a sale already made.

    The ledger rows are named by reference: the buyer's charge (buyer -> escrow), the
    trainer's payout (escrow -> trainer; the credit leg of an exchange when the two
    currencies differ) and the platform's commission (escrow -> platform revenue). All
    three are ROUTINE_PRODUCT and are written in one transaction with this row.
    """
    buyer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                              related_name="routine_purchases")
    product = models.ForeignKey(RoutineProduct, on_delete=models.PROTECT,
                                related_name="purchases")
    status = models.CharField(max_length=16, choices=PurchaseStatus.choices,
                              default=PurchaseStatus.CONFIRMED, db_index=True)
    #: The product's price of record at the sale.
    price_amount = models.DecimalField(max_digits=12, decimal_places=2)
    price_currency = models.CharField(max_length=8)
    #: What the buyer paid, in THEIR currency (the price converted when the two differ).
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=8)
    #: The operator's rate the price was converted at; null when nothing was converted.
    exchange_rate = models.ForeignKey("wallet.ExchangeRate", null=True, blank=True,
                                      on_delete=models.PROTECT, related_name="+")
    exchange_rate_value = models.DecimalField(max_digits=18, decimal_places=6,
                                              null=True, blank=True)
    #: `COACHING_COMMISSION_RATE` when the sale was made (the spec: it applies to
    #: ROUTINE_PRODUCT sales too), and the split of `amount` it produced
    #: (`users.coaching.split`: the two always add up to `amount`).
    commission_rate = models.DecimalField(max_digits=5, decimal_places=4)
    commission = models.DecimalField(max_digits=12, decimal_places=2)
    trainer_share = models.DecimalField(max_digits=12, decimal_places=2)
    #: What landed in the trainer's wallet, in the trainer's currency — `trainer_share`
    #: itself unless the share had to cross a currency.
    trainer_amount = models.DecimalField(max_digits=12, decimal_places=2)
    trainer_currency = models.CharField(max_length=8)
    charge_reference = models.CharField(max_length=36, blank=True, default="")
    payout_reference = models.CharField(max_length=36, blank=True, default="")
    commission_reference = models.CharField(max_length=36, blank=True, default="")
    #: The buyer's key for this purchase. A retry with the same key answers this row.
    idempotency_key = models.CharField(max_length=64)
    paid_with = models.CharField(max_length=16, choices=PaidWith.choices,
                                 default=PaidWith.WALLET)
    #: The hosted checkout's reference once one exists (`paid_with="shamcash"`); blank
    #: for a wallet sale. The seam, not a feature: nothing writes it yet.
    checkout_reference = models.CharField(max_length=64, blank=True, default="")
    refunded_at = models.DateTimeField(null=True, blank=True)
    refunded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name="+")
    refund_reason = models.TextField(max_length=1000, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = "Routine purchase"
        verbose_name_plural = "Routine purchases"
        constraints = [
            # One key, one purchase, per buyer: the database's half of idempotency.
            models.UniqueConstraint(fields=["buyer", "idempotency_key"],
                                    name="routine_purchase_key_per_buyer"),
            # Buying twice is refused (409 `already_owned`) before anything moves; this
            # is the backstop for two purchases racing past that check. A refunded
            # purchase does not count: the buyer may buy the program again.
            models.UniqueConstraint(fields=["buyer", "product"],
                                    condition=Q(status="confirmed"),
                                    name="routine_purchase_one_confirmed"),
        ]
        indexes = [models.Index(fields=["product", "status"],
                                name="routinepurchase_product_idx")]

    def __str__(self):
        return f"Purchase {self.pk} of product {self.product_id}"

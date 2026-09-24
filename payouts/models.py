"""A trainer's payout methods and withdrawals (the spec, section 8 "Trainer payouts";
BACKEND_BUGS B-098; the design is `15-trainer-money/TrainerWithdraw`,
`TrainerWithdrawStatus` and `TrainerPayoutMethod`).

**Payout method.** Where a trainer's money goes: a ShamCash ADDRESS (typed twice, with
the holder's name) or cash collected from a NAMED agent (`wallet.AgentProfile`, listed
and active). An address, never an account: no password, PIN, OTP or session is asked
for or stored, on any screen (spec section 6, "Save the address, not the account"). The
address and the holder's name are personal data, encrypted at rest like
`TrainerClientRelation.client_note`; `address_digest` (a keyed hash) is what lets the
database refuse the same address twice, since a randomised ciphertext cannot be
compared. A trainer may keep several and marks one default.

**Withdrawal.** States, one direction (spec section 8):

    requested ──approve──▶ approved ──sent──▶ sent ──confirm──▶ confirmed
        │                     │                 │
        ├──cancel (trainer)───┤                 │
        │                     └────── fail ─────┴──▶ failed
        └──────────── fail ───────────────────────▶ failed
    requested ──cancel──▶ cancelled

The amount leaves the trainer's wallet for the payout-clearing wallet AT THE REQUEST
(`hold_reference`), so it cannot be spent or withdrawn twice; `failed` and `cancelled`
move it back (`return_reference`), and `confirmed` moves it out of clearing, off the
platform (`payout_reference`). Where it went is COPIED onto the withdrawal
(`destination_*`) when it is requested, so a method the trainer edits or deletes
afterwards changes nothing in flight ("يُطبَّق على الطلبات الجديدة").

**Events.** Every transition writes a `WithdrawalEvent` — who or what, when, from, to,
evidence — that is never updated or deleted: `save()` and `delete()` refuse, and a
database trigger refuses both for every other path (migration 0002).

State changes only in `payouts/services.py`.
"""
from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from training_platform.encrypted_fields import EncryptedTextField


class MethodKind(models.TextChoices):
    SHAMCASH = "shamcash", _("ShamCash")
    AGENT_CASH = "agent_cash", _("Cash from an agent")


class State(models.TextChoices):
    REQUESTED = "requested", _("Requested")
    APPROVED = "approved", _("Approved")
    SENT = "sent", _("Sent")
    CONFIRMED = "confirmed", _("Arrived")
    FAILED = "failed", _("Failed")
    CANCELLED = "cancelled", _("Cancelled")


#: A withdrawal whose money is in payout clearing: at most one per trainer.
OPEN_STATES = (State.REQUESTED, State.APPROVED, State.SENT)


class ActorRole(models.TextChoices):
    TRAINER = "trainer", "Trainer"
    OPERATOR = "operator", "Operator"
    SYSTEM = "system", "System"


class PayoutMethod(models.Model):
    trainer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                related_name="payout_methods")
    kind = models.CharField(max_length=16, choices=MethodKind.choices)
    #: The ShamCash account ADDRESS, normalised (no spaces or dashes, lower case).
    #: Encrypted: nothing filters on it — `address_digest` is what is compared.
    shamcash_address = EncryptedTextField(blank=True, default="")
    #: A keyed hash of the normalised address (`services.address_digest`), so the
    #: database can refuse one address saved twice by the same trainer.
    address_digest = models.CharField(max_length=64, blank=True, default="")
    #: The name on the ShamCash account, as the trainer typed it. Encrypted.
    holder_name = EncryptedTextField(blank=True, default="")
    #: For cash collection: the agent the trainer collects from.
    agent = models.ForeignKey("wallet.AgentProfile", on_delete=models.PROTECT, null=True,
                              blank=True, related_name="+")
    is_default = models.BooleanField(default=False)
    #: When money sent to this address was confirmed to have arrived — the design's
    #: «محقّق», and the spec's "we verify the address with a small first transfer".
    verified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-is_default", "-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["trainer"], condition=Q(is_default=True),
                                    name="one_default_payout_method_per_trainer"),
            models.UniqueConstraint(fields=["trainer", "address_digest"],
                                    condition=Q(kind="shamcash"),
                                    name="one_payout_method_per_address"),
            models.UniqueConstraint(fields=["trainer", "agent"],
                                    condition=Q(kind="agent_cash"),
                                    name="one_payout_method_per_agent"),
            models.CheckConstraint(
                condition=(Q(kind="shamcash", agent__isnull=True) & ~Q(address_digest=""))
                | Q(kind="agent_cash", agent__isnull=False, address_digest=""),
                name="payout_method_is_an_address_or_an_agent"),
        ]

    def __str__(self):
        return f"{self.kind} #{self.pk} of trainer {self.trainer_id}"


class Withdrawal(models.Model):
    #: "WD-2026-09-23-041": the local date of the request and its number that day
    #: (`TrainerWithdrawStatus`). What the trainer quotes to support.
    number = models.CharField(max_length=32, unique=True)
    trainer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                related_name="withdrawals")
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    #: The trainer's wallet's, which is the trainer's (`wallet.currency_for`).
    currency = models.CharField(max_length=8)
    state = models.CharField(max_length=16, choices=State.choices,
                             default=State.REQUESTED, db_index=True)
    #: The method chosen; null once the trainer deletes it. What was actually used is
    #: the copy below.
    method = models.ForeignKey(PayoutMethod, on_delete=models.SET_NULL, null=True,
                               blank=True, related_name="withdrawals")
    destination_kind = models.CharField(max_length=16, choices=MethodKind.choices)
    destination_address = EncryptedTextField(blank=True, default="")
    destination_holder = EncryptedTextField(blank=True, default="")
    destination_agent = models.ForeignKey("wallet.AgentProfile", on_delete=models.PROTECT,
                                          null=True, blank=True, related_name="+")
    #: The ShamCash transaction the operator sent it with. ONE transfer pays ONE
    #: withdrawal (spec section 7, "store the ShamCash transaction id with a unique
    #: constraint").
    shamcash_transaction_id = models.CharField(max_length=64, null=True, blank=True,
                                               unique=True)
    #: The ledger rows, by `Transaction.reference_id`: trainer -> clearing at the
    #: request; clearing -> trainer on failure or cancellation; clearing -> out when it
    #: arrived.
    hold_reference = models.CharField(max_length=36, blank=True, default="")
    return_reference = models.CharField(max_length=36, blank=True, default="")
    payout_reference = models.CharField(max_length=36, blank=True, default="")
    #: Why it failed, in the operator's words; shown to the trainer.
    failure_reason = models.TextField(blank=True, default="")
    requested_at = models.DateTimeField(default=timezone.now, db_index=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    failed_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-requested_at", "-id"]
        indexes = [
            models.Index(fields=["trainer", "-requested_at", "-id"],
                         name="withdrawal_trainer_recent_idx"),
            models.Index(fields=["state", "requested_at", "id"],
                         name="withdrawal_queue_idx"),
        ]
        constraints = [
            # "One open request per trainer" (spec section 8), said by the database.
            models.UniqueConstraint(fields=["trainer"],
                                    condition=Q(state__in=[s.value for s in OPEN_STATES]),
                                    name="one_open_withdrawal_per_trainer"),
            models.CheckConstraint(condition=Q(amount__gt=0),
                                   name="withdrawal_amount_positive"),
            models.CheckConstraint(condition=Q(state__in=State.values),
                                   name="withdrawal_state_known"),
        ]

    def __str__(self):
        return self.number

    @property
    def is_open(self) -> bool:
        return self.state in OPEN_STATES


class WithdrawalQueueEntry(Withdrawal):
    """The operator's queue: the open withdrawals, oldest request first
    (`admin.WithdrawalQueueAdmin`). A proxy, so it is its own console page."""

    class Meta:
        proxy = True
        ordering = ["requested_at", "id"]
        verbose_name = "Withdrawal queue entry"
        verbose_name_plural = "Withdrawal queue (open, oldest first)"


class ImmutableEvent(PermissionError):
    pass


class WithdrawalEvent(models.Model):
    """One transition of one withdrawal, never edited. See the module docstring."""
    withdrawal = models.ForeignKey(Withdrawal, on_delete=models.PROTECT,
                                   related_name="events")
    #: Empty for the request itself.
    from_state = models.CharField(max_length=16, blank=True, default="")
    to_state = models.CharField(max_length=16, choices=State.choices)
    #: Who did it: the trainer (request, cancel) or an operator. Null for the system,
    #: or once that account is gone; `actor_role` still says which.
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                              null=True, blank=True, related_name="+")
    actor_role = models.CharField(max_length=16, choices=ActorRole.choices)
    #: What it rested on: the ledger reference it wrote, the ShamCash transaction id,
    #: the operator's note or reason, the idempotency key of the request.
    evidence = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [models.Index(fields=["withdrawal", "created_at", "id"],
                                name="withdrawal_event_timeline_idx")]

    def __str__(self):
        return f"{self.withdrawal_id}: {self.from_state or '-'} -> {self.to_state}"

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ImmutableEvent("a withdrawal event is never edited; write a new one")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ImmutableEvent("a withdrawal event is never deleted")

"""What each build may show and sell, decided by the server.

One row per (channel, minimum app version). The app sends its channel on every request
and renders only what the row for it allows, so a policy change is a row edit rather
than a release. Three rules keep that from becoming a way to deceive a store reviewer:

* A row describes a BUILD, never a user. Nothing here can vary by account, country, IP
  or date, because the lookup takes a channel and a version and nothing else.
* What a store build may never do is not a flag. `STORE_LOCKED_FLAGS` cannot be switched
  on for `play` or `ios` — refused when the row is saved and masked again when it is
  read — so no edit here can turn purchasing on inside a build a store has reviewed.
* Every change is written to `CapabilityChangeLog` in the same transaction: who, when,
  old and new.
"""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils.translation import gettext_lazy as _

from training_platform.model_validation import RowValidationMixin

from .channels import STORE_CHANNELS, Channel, parse_version

#: Every flag the capabilities endpoint returns, in the order it returns them.
FLAG_FIELDS = (
    "can_purchase_platform_sub",
    "can_purchase_routine_product",
    "can_pay_coaching_in_app",
    "can_show_prices",
    "can_show_coaching_prices",
    "can_show_web_hub_link",
    "can_view_entitlements",
    "can_view_wallet",
    "can_transfer_wallet",
)

#: Off for every store build, whatever a row says. Plans and routine products are digital
#: goods both stores bill themselves; a link to the web hub, or a price beside them, is a
#: call to pay elsewhere. `can_show_coaching_prices` is NOT here: a trainer's fee is a
#: person-to-person service both stores let an app charge for directly.
STORE_LOCKED_FLAGS = (
    "can_purchase_platform_sub",
    "can_purchase_routine_product",
    "can_show_prices",
    "can_show_web_hub_link",
)


class ChannelCapabilities(RowValidationMixin, models.Model):
    channel = models.CharField(max_length=8, choices=Channel.choices, db_index=True)
    min_version = models.CharField(
        max_length=17, default="0.0.0",
        help_text=_("Applies to builds at or above this version, until a row with a "
                    "higher version takes over. 0.0.0 is the channel's base row."),
    )

    # Restrictive by default: a row somebody adds and forgets to fill in sells nothing.
    can_purchase_platform_sub = models.BooleanField(default=False)
    can_purchase_routine_product = models.BooleanField(default=False)
    can_pay_coaching_in_app = models.BooleanField(default=False)
    can_show_prices = models.BooleanField(
        default=False, help_text=_("Prices of plans and routine products."))
    can_show_coaching_prices = models.BooleanField(
        default=False, help_text=_("A trainer's own fee for one-to-one coaching."))
    can_show_web_hub_link = models.BooleanField(default=False)
    can_view_entitlements = models.BooleanField(default=True)
    can_view_wallet = models.BooleanField(default=True)
    can_transfer_wallet = models.BooleanField(default=False)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["channel", "min_version", "id"]
        constraints = [
            models.UniqueConstraint(fields=["channel", "min_version"],
                                    name="uniq_capabilities_channel_version"),
        ]
        verbose_name = "Channel capabilities"
        verbose_name_plural = "Channel capabilities"

    def __str__(self):
        return f"{self.channel} >= {self.min_version}"

    @property
    def version_tuple(self):
        return parse_version(self.min_version) or (0, 0, 0)

    def flags(self) -> dict:
        """The flags as this row stores them. Readers want `policy.services.flags_for`."""
        return {name: getattr(self, name) for name in FLAG_FIELDS}

    def clean(self):
        parsed = parse_version(self.min_version)
        if parsed is None:
            raise ValidationError({"min_version": _("Use up to three dotted numbers, like 1.4.0")})
        if self.channel in STORE_CHANNELS:
            switched_on = [name for name in STORE_LOCKED_FLAGS if getattr(self, name)]
            if switched_on:
                raise ValidationError({
                    name: _("A store build can never do this. It is fixed in code, not "
                            "in this table.") for name in switched_on})

    def save(self, *args, changed_by=None, **kwargs):
        parsed = parse_version(self.min_version)
        if parsed is not None:
            # One spelling per version, so "1.4" and "1.4.0" cannot both exist.
            self.min_version = ".".join(str(p) for p in parsed)
        self.validate_row()
        with transaction.atomic():
            before = None
            if self.pk:
                before = (type(self).objects.select_for_update()
                          .filter(pk=self.pk).first())
            super().save(*args, **kwargs)
            old = before.flags() if before else {}
            new = self.flags()
            if before is None or old != new:
                CapabilityChangeLog.objects.create(
                    channel=self.channel, min_version=self.min_version,
                    action="created" if before is None else "changed",
                    changed_by=changed_by, old_values=old, new_values=new)

    def delete(self, *args, changed_by=None, **kwargs):
        with transaction.atomic():
            CapabilityChangeLog.objects.create(
                channel=self.channel, min_version=self.min_version, action="deleted",
                changed_by=changed_by, old_values=self.flags(), new_values={})
            return super().delete(*args, **kwargs)


class CapabilityChangeLog(models.Model):
    """Append-only record of every change to `ChannelCapabilities`.

    Holds the channel and version as plain columns rather than a foreign key, so the
    record of a row outlives the row.
    """
    ACTIONS = [("created", "Created"), ("changed", "Changed"), ("deleted", "Deleted")]

    channel = models.CharField(max_length=8, choices=Channel.choices)
    min_version = models.CharField(max_length=17)
    action = models.CharField(max_length=8, choices=ACTIONS)
    #: NULL when the change did not come from a signed-in operator: a migration, a
    #: management command, a shell.
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL,
                                   related_name="capability_changes")
    old_values = models.JSONField(default=dict, blank=True)
    new_values = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = "Capability change"
        verbose_name_plural = "Capability changes"

    def __str__(self):
        return f"{self.channel} >= {self.min_version} {self.action} at {self.created_at:%Y-%m-%d %H:%M}"

    def save(self, *args, **kwargs):
        if self.pk:
            raise PermissionError("CapabilityChangeLog is append-only and cannot be edited.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionError("CapabilityChangeLog is append-only and cannot be deleted.")

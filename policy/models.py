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


class ThemeOverride(RowValidationMixin, models.Model):
    """A skin the server can hand a build — a Ramadan or launch-week look, no release.

    Same discipline as `ChannelCapabilities` above, for the same reason: a row
    describes a BUILD (`channels`, `min_version`..`max_version`) and a window of time
    the operator chose, and nothing in the lookup can see the account, the country, the
    IP or the device's clock. `policy.theme` has the full reasoning on why a scheduled
    skin is still "the same answer for everyone on this build".

    Two guards run when the row is SAVED, so a bad edit cannot ship and the operator
    learns why at the moment they can fix it:

    * `tokens` may only name tokens the design system declares, in the four kinds a
      look is made of — colour, spacing, radius, typography. Control sizes and shadows
      are not overridable; see `policy.theme_tokens`.
    * the merged palette must keep every text/background pair readable (4.5:1, and
      3:1 for the three tokens the design system itself puts at that floor).

    A theme carries no words and no links. Every value is a colour literal, a length
    or a weight, and `name` is an operator's label that is never serialised — so the
    store rule that a remote theme may not carry a price, a purchase button or a
    payment link into a `play` or `ios` build holds by construction, not by review.
    """

    name = models.CharField(
        max_length=60,
        help_text=_("What the operator calls this skin. Never sent to the app."))
    channels = models.JSONField(
        default=list,
        help_text=_('Which builds this applies to, as a list, e.g. ["apk", "play"].'))
    min_version = models.CharField(
        max_length=17, default="0.0.0",
        help_text=_("Applies to builds at or above this version. 0.0.0 is every build."))
    max_version = models.CharField(
        max_length=17, blank=True, default="",
        help_text=_("Applies to builds at or below this version. Blank is no ceiling."))
    starts_at = models.DateTimeField(
        null=True, blank=True,
        help_text=_("When the server starts serving it. Blank means already."))
    ends_at = models.DateTimeField(
        null=True, blank=True,
        help_text=_("When the server stops. Blank means it does not expire. Sent to "
                    "the app as valid_until so it can drop its cache by itself."))
    is_enabled = models.BooleanField(
        default=False,
        help_text=_("Off until an operator turns it on, so a half-written row is "
                    "never served."))
    priority = models.IntegerField(
        default=0,
        help_text=_("When two rows match one build, the higher number wins."))
    tokens = models.JSONField(
        default=dict,
        help_text=_('Token name to value, e.g. {"ember": {"dark": "#FF5A1F"}}.'))

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-priority", "-id"]
        verbose_name = "Theme override"
        verbose_name_plural = "Theme overrides"

    def __str__(self):
        return f"{self.name} ({', '.join(self.channels or [])})"

    # ------------------------------------------------------------------ resolution
    @property
    def min_version_tuple(self):
        return parse_version(self.min_version) or (0, 0, 0)

    @property
    def max_version_tuple(self):
        return parse_version(self.max_version) if self.max_version else None

    def applies_to(self, channel: str, version: tuple, now) -> bool:
        """Whether this row is the answer for a build, at this instant. No user here."""
        if channel not in (self.channels or []):
            return False
        if version < self.min_version_tuple:
            return False
        ceiling = self.max_version_tuple
        if ceiling is not None and version > ceiling:
            return False
        if self.starts_at is not None and now < self.starts_at:
            return False
        if self.ends_at is not None and now >= self.ends_at:
            return False
        return True

    def snapshot(self) -> dict:
        """What the change log records. JSON-safe: datetimes go in as text."""
        return {
            "name": self.name,
            "channels": list(self.channels or []),
            "min_version": self.min_version,
            "max_version": self.max_version,
            "starts_at": self.starts_at.isoformat() if self.starts_at else None,
            "ends_at": self.ends_at.isoformat() if self.ends_at else None,
            "is_enabled": self.is_enabled,
            "priority": self.priority,
            "tokens": dict(self.tokens or {}),
        }

    # ------------------------------------------------------------------ validation
    def clean(self):
        from .theme_tokens import contrast_failures, validate_token_map

        if not isinstance(self.channels, list) or not self.channels:
            raise ValidationError({"channels": _(
                "Name at least one channel, from: %(channels)s."
            ) % {"channels": ", ".join(Channel.values)}})
        unknown = [c for c in self.channels if c not in Channel.values]
        if unknown:
            raise ValidationError({"channels": _(
                "There is no channel called %(channel)s. The channels are "
                "%(channels)s."
            ) % {"channel": unknown[0], "channels": ", ".join(Channel.values)}})

        if parse_version(self.min_version) is None:
            raise ValidationError({"min_version": _(
                "Use up to three dotted numbers, like 1.4.0")})
        if self.max_version and parse_version(self.max_version) is None:
            raise ValidationError({"max_version": _(
                "Use up to three dotted numbers, like 1.4.0")})
        ceiling = self.max_version_tuple
        if ceiling is not None and ceiling < self.min_version_tuple:
            raise ValidationError({"max_version": _(
                "The highest version cannot be below the lowest one.")})

        if self.starts_at and self.ends_at and self.ends_at <= self.starts_at:
            raise ValidationError({"ends_at": _("The end must come after the start.")})

        validate_token_map(self.tokens)
        failures = contrast_failures(self.tokens or {})
        if failures:
            raise ValidationError({"tokens": failures})

    def save(self, *args, changed_by=None, **kwargs):
        for field in ("min_version", "max_version"):
            parsed = parse_version(getattr(self, field))
            if parsed is not None:
                # One spelling per version, as `ChannelCapabilities` does it.
                setattr(self, field, ".".join(str(p) for p in parsed))
        if isinstance(self.channels, list):
            self.channels = sorted(set(self.channels))
        self.validate_row()
        with transaction.atomic():
            before = None
            if self.pk:
                before = (type(self).objects.select_for_update()
                          .filter(pk=self.pk).first())
            super().save(*args, **kwargs)
            old = before.snapshot() if before else {}
            new = self.snapshot()
            if before is None or old != new:
                ThemeChangeLog.objects.create(
                    theme_id_at_the_time=self.pk, name=self.name,
                    action="created" if before is None else "changed",
                    changed_by=changed_by, old_values=old, new_values=new)
            transaction.on_commit(_bump_theme_cache)

    def delete(self, *args, changed_by=None, **kwargs):
        with transaction.atomic():
            ThemeChangeLog.objects.create(
                theme_id_at_the_time=self.pk, name=self.name, action="deleted",
                changed_by=changed_by, old_values=self.snapshot(), new_values={})
            transaction.on_commit(_bump_theme_cache)
            return super().delete(*args, **kwargs)


def _bump_theme_cache():
    from .theme import bump_generation

    bump_generation()


class ThemeChangeLog(models.Model):
    """Append-only record of every change to `ThemeOverride`.

    Holds the theme's id and name as plain columns, so the record of a skin outlives
    the row — the same shape as `CapabilityChangeLog`.
    """
    ACTIONS = [("created", "Created"), ("changed", "Changed"), ("deleted", "Deleted")]

    theme_id_at_the_time = models.IntegerField(null=True, blank=True)
    name = models.CharField(max_length=60, blank=True)
    action = models.CharField(max_length=8, choices=ACTIONS)
    #: NULL when the change did not come from a signed-in operator.
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL,
                                   related_name="theme_changes")
    old_values = models.JSONField(default=dict, blank=True)
    new_values = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = "Theme change"
        verbose_name_plural = "Theme changes"

    def __str__(self):
        return f"{self.name} {self.action} at {self.created_at:%Y-%m-%d %H:%M}"

    def save(self, *args, **kwargs):
        if self.pk:
            raise PermissionError("ThemeChangeLog is append-only and cannot be edited.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionError("ThemeChangeLog is append-only and cannot be deleted.")


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

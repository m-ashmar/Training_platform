"""The registry itself."""
from __future__ import annotations

import logging

from django.core.exceptions import FieldError
from dataclasses import dataclass, field
from typing import Callable, Iterable

logger = logging.getLogger(__name__)


@dataclass
class PersonalDataSource:
    """One model's contribution to a user's personal data.

    ``label``        human name used in the export
    ``model``        "app_label.ModelName"
    ``user_field``   how the row is tied to a user ("user", "author", "session__user")
    ``fields``       what to include in an export; None means every concrete field
    ``on_erase``     "delete" | "anonymise" | "keep"
    ``anonymise``    field -> value (or callable) applied when on_erase="anonymise"
    ``retention_days`` rows older than this are removed by purge_expired(); None = keep
    ``retention_field`` timestamp the retention window is measured from
    """

    label: str
    model: str
    user_field: str
    fields: list[str] | None = None
    on_erase: str = "delete"
    anonymise: dict[str, object] = field(default_factory=dict)
    retention_days: int | None = None
    retention_field: str = "created_at"

    def get_model(self):
        from django.apps import apps

        return apps.get_model(*self.model.split("."))

    def queryset_for(self, user):
        # `pk`/`id` take the primary key value; a ForeignKey takes the instance. Passing
        # the instance to `pk` raises "Field 'id' expected a number".
        value = user.pk if self.user_field in ("pk", "id") else user
        return self.get_model()._default_manager.filter(**{self.user_field: value})


registry: dict[str, PersonalDataSource] = {}


def register(source: PersonalDataSource) -> PersonalDataSource:
    registry[source.model] = source
    return source


# ---------------------------------------------------------------- Art. 15: access
def export_user_data(user) -> dict:
    """Everything the platform holds about `user`, as JSON-serialisable data."""
    from django.core.serializers.json import DjangoJSONEncoder  # noqa: F401
    from django.utils import timezone

    out = {
        "generated_at": timezone.now().isoformat(),
        "user_id": user.pk,
        "sections": {},
    }
    for source in registry.values():
        try:
            qs = source.queryset_for(user)
            names = source.fields or [
                f.name for f in source.get_model()._meta.get_fields()
                if getattr(f, "concrete", False)
            ]
            out["sections"][source.label] = list(qs.values(*names))
        except Exception:
            # One unexportable model must not deny the whole request.
            logger.exception("Export failed for %s", source.model)
            out["sections"][source.label] = {"error": "could not be exported"}
    return out


# --------------------------------------------------------------- Art. 17: erasure
def erase_user_data(user, *, dry_run: bool = False) -> dict:
    """Remove or anonymise personal data, preserving anything financial.

    Wallets, transactions, payments and the audit chain are registered with
    ``on_erase="keep"``: they are financial records, and `Wallet.owner` is PROTECT
    precisely so a deletion cannot erase a balance.
    """
    from django.db import transaction

    report: dict[str, dict] = {}

    def _run():
        for source in registry.values():
            qs = source.queryset_for(user)
            count = qs.count()
            action = source.on_erase
            if count and not dry_run:
                if action == "delete":
                    qs.delete()
                elif action == "anonymise":
                    values = {
                        k: (v(user) if callable(v) else v)
                        for k, v in source.anonymise.items()
                    }
                    # save() per row, not queryset.update(): update() skips every signal,
                    # so clearing an ImageField would leave the file on disk forever —
                    # the same defect found in the admin's bulk actions.
                    for obj in qs:
                        for key, value in values.items():
                            setattr(obj, key, value)
                        obj.save(update_fields=list(values))
            report[source.label] = {"rows": count, "action": action}

    if dry_run:
        _run()
    else:
        with transaction.atomic():
            _run()
    return report


# ------------------------------------------------------- storage limitation
def purge_expired() -> dict:
    """Delete rows past their retention window, across every registered source."""
    from datetime import timedelta

    from django.utils import timezone

    removed: dict[str, int] = {}
    for source in registry.values():
        if not source.retention_days:
            continue
        cutoff = timezone.now() - timedelta(days=source.retention_days)
        try:
            qs = source.get_model()._default_manager.filter(
                **{f"{source.retention_field}__lt": cutoff}
            )
            n, _detail = qs.delete()
            if n:
                removed[source.label] = n
        except FieldError:
            # The source is misconfigured, not merely unlucky. Every future run fails
            # the same way, so this must be shouted rather than filed under "failed".
            logger.critical(
                "Retention source %s is misconfigured: no field %r on %s. These rows "
                "are never purged. See privacy.validate_sources().",
                source.label, source.retention_field, source.model,
            )
        except Exception:
            logger.exception("Retention purge failed for %s", source.model)
    return removed


# ----------------------------------------------------------------- self-check
def validate_sources() -> list[str]:
    """Every declared field must exist on the model it is declared against.

    A source is a declaration, and nothing was checking it against reality. One
    retention_field named a column that did not exist; purge_expired() caught the
    FieldError, logged it and carried on, so the retention job reported success while
    that source was never purged at all. A declaration nobody verifies is the same
    defect as a scheduled task nobody registered.
    """
    problems: list[str] = []
    for source in registry.values():
        try:
            model = source.get_model()
        except Exception as exc:
            problems.append(f"{source.label}: model {source.model!r} does not resolve ({exc})")
            continue

        names = {f.name for f in model._meta.get_fields()}
        # `pk`/`id` are valid lookups even though `pk` is not a field name.
        head = source.user_field.split("__")[0]
        if head not in names and head not in ("pk", "id"):
            problems.append(
                f"{source.label}: user_field {source.user_field!r} is not on {source.model}"
            )
        if source.retention_days and source.retention_field not in names:
            problems.append(
                f"{source.label}: retention_field {source.retention_field!r} is not on "
                f"{source.model} — retention would silently never run"
            )
        for fname in source.fields or []:
            if fname not in names and fname not in ("pk", "id"):
                problems.append(
                    f"{source.label}: exported field {fname!r} is not on {source.model}"
                )
        for fname in (source.anonymise or {}):
            if fname not in names:
                problems.append(
                    f"{source.label}: anonymise field {fname!r} is not on {source.model}"
                )
    return problems


# ------------------------------------------------------------------- coverage
def audit_coverage() -> list[str]:
    """Models holding a user FK that no source claims.

    This is what stops the registry rotting: a model added later that nobody registered
    shows up here instead of being quietly excluded from export, erasure and retention.
    """
    from django.apps import apps
    from django.db import models as M

    local = {"users", "wallet", "diet", "routine", "subscription", "social",
             "ai_assistant", "achievements", "analytics", "notifications", "challenges"}
    claimed = set(registry)
    missing = []
    for model in apps.get_models():
        if model._meta.app_label not in local:
            continue
        # Proxy models share their concrete model's table; registering both would
        # export the same rows twice and delete them twice.
        if model._meta.proxy:
            continue
        dotted = f"{model._meta.app_label}.{model.__name__}"
        if dotted in claimed:
            continue
        has_user_fk = any(
            isinstance(f, M.ForeignKey) and f.related_model._meta.label_lower.endswith("customuser")
            for f in model._meta.get_fields()
        )
        if has_user_fk:
            missing.append(dotted)
    return sorted(missing)

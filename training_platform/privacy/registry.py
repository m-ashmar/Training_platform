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
    ``user_field``   how the row is tied to a user ("user", "author", "session__user").
                     A tuple names SEVERAL paths, for a model that reaches its owner by
                     more than one nullable route; a row matching any of them belongs to
                     that user.
    ``fields``       what to include in an export; None means every concrete field
    ``on_erase``     "delete" | "anonymise" | "keep"
    ``anonymise``    field -> value (or callable) applied when on_erase="anonymise"
    ``retention_days`` rows older than this are removed by purge_expired(); None = keep
    ``retention_field`` timestamp the retention window is measured from
    """

    label: str
    model: str
    user_field: str | tuple[str, ...]
    fields: list[str] | None = None
    on_erase: str = "delete"
    anonymise: dict[str, object] = field(default_factory=dict)
    retention_days: int | None = None
    retention_field: str = "created_at"
    #: What `retention_field` MEANS. False (the default) is "when this row was written",
    #: and rows older than `retention_days` go. True is "when this row must be gone by" —
    #: a date in the future — and the row goes once that date passes.
    #:
    #: Without the distinction the two were treated the same, and `ai_training_data`
    #: writes `retain_until = now + 365 days`: `purge_expired` then deleted rows whose
    #: deadline was more than 365 days in the PAST, i.e. 730 days after writing. Special
    #: category health data — injury text, weight, BMI, goals — kept for twice the
    #: declared window, in the one source where the declared window is the promise.
    retention_is_deadline: bool = False
    #: Runs inside the erasure transaction, before ANY source is deleted or anonymised,
    #: as `before_erase(user, queryset, dry_run=...)`. For a row that is personal data
    #: and also a record of money: the obligation has to be discharged while the row
    #: that names it still exists. May return a dict, merged into this source's report
    #: (a dry run reports what it would do); raises `ErasureRefused` to abort the whole
    #: erasure.
    #:
    #: The registry had no such step, so `TrainerClientRelation` — whose
    #: `hold_reference` is the only record of whose money is in escrow — went through a
    #: bulk `qs.delete()`. Measured: approve, escrow 80.00; erase, 200; relation gone,
    #: escrow still 80.00, no release in the ledger, and no code path left that could
    #: ever refund it. A trainer's erasure did the same to every client still waiting
    #: for a routine, while those clients stayed active and were told "Successfully
    #: unassigned".
    before_erase: Callable | None = None

    def get_model(self):
        from django.apps import apps

        return apps.get_model(*self.model.split("."))

    def queryset_for(self, user):
        from django.db.models import Q

        paths = ((self.user_field,) if isinstance(self.user_field, str)
                 else tuple(self.user_field))
        # `pk`/`id` take the primary key value; a ForeignKey takes the instance. Passing
        # the instance to `pk` raises "Field 'id' expected a number".
        query = Q()
        for path in paths:
            query |= Q(**{path: user.pk if path in ("pk", "id") else user})
        return self.get_model()._default_manager.filter(query).distinct()


registry: dict[str, PersonalDataSource] = {}


class ErasureRefused(Exception):
    """A `before_erase` step could not discharge what the rows it guards stand for.

    Raised out of the erasure transaction, so nothing at all is erased: deleting the
    record of an obligation that could not be met is the one outcome worse than
    refusing, because it cannot be retried.

    `code` tells the refusals apart for the app: `erasure_refused` (a held payment could
    not be returned; retry later) or `trainer_has_active_clients` (the owner's rule: end
    coaching with every client first). `extra` goes into the response body.
    """

    def __init__(self, message="", *, code="erasure_refused", **extra):
        super().__init__(message)
        self.code = code
        self.extra = extra


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
            model = source.get_model()
            # Concrete COLUMNS only. `ManyToManyField.concrete` is True, so this list
            # used to include every M2M on the model and `.values()` turned each one into
            # a LEFT JOIN: `diet.UserFoodPreference` has seven, and one client with eight
            # likes, eight dislikes and four foods in each of five choice lists produced
            # 8 x 8 x 4^5 = 65,536 rows for a single record. The request hung, the broad
            # handler below filed the section as an error, and the person received a
            # silently incomplete export of their own data.
            names = source.fields or [
                f.name for f in model._meta.get_fields()
                if getattr(f, "concrete", False) and not f.many_to_many
            ]
            rows = list(qs.values(*names))
            # The M2M contents are still theirs. Attached as lists of ids, one extra
            # query per relation, instead of multiplying the row out.
            links = [f for f in model._meta.get_fields()
                     if f.many_to_many and getattr(f, "concrete", False)]
            if links and rows:
                for row, obj in zip(rows, qs):
                    for link in links:
                        row[link.name] = list(
                            getattr(obj, link.name).values_list("pk", flat=True))
            out["sections"][source.label] = rows
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
        # Every `before_erase` first, before any row is touched. Besides needing the
        # rows they guard to still exist, these take row and wallet locks, and taking
        # them ahead of the profile anonymisation keeps erasure in the same lock order
        # as approval and settlement (relation, user rows, wallets) instead of holding
        # the user row while it waits for a relation an approval has locked.
        extra: dict[str, dict] = {}
        for source in registry.values():
            if source.before_erase is not None:
                extra[source.label] = source.before_erase(
                    user, source.queryset_for(user), dry_run=dry_run) or {}

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
            report[source.label] = {"rows": count, "action": action,
                                    **extra.get(source.label, {})}

    if dry_run:
        _run()
    else:
        with transaction.atomic():
            _run()
    return report


# ------------------------------------------------------- storage limitation
#: How many rows one DELETE covers. Small enough that the id list and the objects
#: Django collects for it stay bounded on a 512 MB worker, large enough that a big
#: purge is still a handful of statements per thousand rows.
PURGE_BATCH = 2000


def _delete_in_batches(queryset) -> int:
    """Delete a queryset a page at a time, and return how many rows went.

    `queryset.delete()` looks cheap and is not. Django can only issue a bare
    `DELETE ... WHERE` when nothing points at the model with a cascade or SET_NULL it
    has to handle in Python; `analytics.PerformanceMetric.related_activity` is SET_NULL
    on `UserActivity`, so `can_fast_delete()` is False and the whole result set is
    materialised first. Measured: 200,000 expiring rows cost 6.7 s and 181 MB of
    resident memory, against a worker `fly.toml` gives 512 MB and runs at
    `--concurrency=2`. `CELERY_TASK_ACKS_LATE` plus `CELERY_TASK_REJECT_ON_WORKER_LOST`
    then redeliver the task to the sibling child, which dies the same way — a crash loop
    that takes every other job down with it, on the first purge after 180 days of
    traffic.

    Batching keeps the peak flat regardless of how far behind the purge has fallen.
    """
    model = queryset.model
    removed = 0
    while True:
        ids = list(queryset.values_list("pk", flat=True)[:PURGE_BATCH])
        if not ids:
            return removed
        n, _detail = model._default_manager.filter(pk__in=ids).delete()
        if not n:
            # Nothing deleted despite matching rows: a PROTECT somewhere. Stop rather
            # than spin.
            logger.warning("purge made no progress on %s; stopping", model.__name__)
            return removed
        removed += n


def purge_expired() -> dict:
    """Delete rows past their retention window, across every registered source."""
    from datetime import timedelta

    from django.db.models import Q
    from django.utils import timezone

    removed: dict[str, int] = {}
    for source in registry.values():
        if not source.retention_days:
            continue
        now = timezone.now()
        cutoff = now if source.retention_is_deadline else (
            now - timedelta(days=source.retention_days))
        try:
            qs = source.get_model()._default_manager.filter(
                **{f"{source.retention_field}__lt": cutoff}
            )
            if source.retention_is_deadline:
                # A row with no deadline is not exempt, it is unaccounted for. Anything
                # older than the window goes too, so rows written before the deadline
                # column existed cannot sit there forever — which is exactly what the 89
                # rows collected before the consent gate were doing: `retain_until` NULL,
                # and both purge queries filter on it.
                # A Q, not two materialised id lists. Building `pk__in=[...]` here read
                # every expiring primary key into worker memory before a single row was
                # deleted — the same shape as the batching fix below, in the one branch
                # that batching could not save.
                qs = source.get_model()._default_manager.filter(
                    Q(**{f"{source.retention_field}__lt": cutoff})
                    | Q(**{f"{source.retention_field}__isnull": True,
                           "created_at__lt": now - timedelta(days=source.retention_days)}))
            n = _delete_in_batches(qs)
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
        # `pk`/`id` are valid lookups even though `pk` is not a field name. A tuple names
        # several routes to the owner; every one of them has to resolve.
        paths = ((source.user_field,) if isinstance(source.user_field, str)
                 else tuple(source.user_field))
        if not paths:
            problems.append(f"{source.label}: user_field is empty")
        for path in paths:
            head = path.split("__")[0]
            if head not in names and head not in ("pk", "id"):
                problems.append(
                    f"{source.label}: user_field {path!r} is not on {source.model}"
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
             "ai_assistant", "achievements", "analytics", "notifications", "challenges",
             "policy", "chat"}
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
        if _reaches_a_user(model):
            missing.append(dotted)
    return sorted(missing)


def _reaches_a_user(model, *, depth: int = 3) -> bool:
    """Whether rows of `model` belong to a person, directly or through a chain of FKs.

    Direct-FK-only was the whole test, and it is why the audit could not see the two
    models that were in fact missed: `diet.Meal` reaches its owner through
    `diet_plan__user` and `diet.MealComponent` through `meal__diet_plan__user`. The gate
    test asserts `audit_coverage() == []`, so it passed whether or not a two-hop model
    was registered, and the next `Recipe`-shaped model hung under `DietPlan` would have
    escaped export, erasure and retention with a green suite.
    """
    from django.db import models as M

    seen = set()

    def walk(current, left):
        if left < 0 or current._meta.label in seen:
            return False
        seen.add(current._meta.label)
        for field in current._meta.get_fields():
            if not isinstance(field, (M.ForeignKey, M.OneToOneField)):
                continue
            related = field.related_model
            if related is None:
                continue
            if related._meta.label_lower.endswith("customuser"):
                return True
            if walk(related, left - 1):
                return True
        return False

    return walk(model, depth)

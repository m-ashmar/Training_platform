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
    ``user_value``   what ``user_field`` is compared with, as ``user_value(user)``; None
                     (the default) means the user itself (its pk for "pk"/"id"). For a
                     source keyed by a pseudonym instead of a FK, such as the anonymous
                     AI training copies (``subject_key``, a keyed hash of the user id)
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
    #: Rows that go with this person on erasure although they are NOT this person's to
    #: read, as `erase_also(user) -> QuerySet` of this source's model. Only for
    #: `on_erase="delete"`. Deleted in the same transaction as the source's own rows,
    #: never exported, and never counted in the erasure report.
    #:
    #: `user_field` is what BOTH operations follow, so a path that erasure needs and the
    #: export must not show had nowhere to go. `moderation.Block` was registered on
    #: `("blocker", "blocked")` so an erasure would clear both sides — and the export
    #: therefore handed a blocked person every block made against them, with the
    #: blocker's id, which the block list itself promises never to disclose. The report
    #: is left out for the same reason: it is shown to the subject (`GET
    #: privacy/erase/` is the preview), and "blocks: 2" to someone who made none says
    #: they were blocked twice.
    erase_also: Callable | None = None
    #: See ``user_value`` in the class docstring. Last, so no positional use shifts.
    user_value: Callable | None = None
    #: How this source's rows are deleted on erasure, as `delete_rows(user, queryset)`,
    #: when a bare `queryset.delete()` is not safe. Only for `on_erase="delete"`; runs in
    #: the erasure transaction, after every `before_erase`, in the source's turn. May
    #: raise `ErasureRefused`, which rolls the whole erasure back.
    #:
    #: A `queryset.delete()` removes the rows and leaves each file to the post-commit
    #: receiver of `training_platform/signals.py`, which can only log a storage error:
    #: the row that knew where the file was is gone by then. For an identity document
    #: that meant an erased trainer's ID card could outlive the erasure, with nothing
    #: left to find it (the verification review of 2026-09-24). Its source deletes the
    #: files first, and refuses the erasure when storage does.
    delete_rows: Callable | None = None

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
            if self.user_value is not None:
                query |= Q(**{path: self.user_value(user)})
            else:
                query |= Q(**{path: user.pk if path in ("pk", "id") else user})
        return self.get_model()._default_manager.filter(query).distinct()


registry: dict[str, PersonalDataSource] = {}


class ErasureRefused(Exception):
    """A `before_erase` step could not discharge what the rows it guards stand for.

    Raised out of the erasure transaction, so nothing at all is erased: deleting the
    record of an obligation that could not be met is the one outcome worse than
    refusing, because it cannot be retried.

    `code` tells the refusals apart for the app: `erasure_refused` (a held payment could
    not be returned, or a stored identity document could not be deleted; retry later)
    or `trainer_has_active_clients` (the owner's rule: end coaching with every client
    first). `extra` says which, for the message: `cause="storage"` for a document.
    """

    def __init__(self, message="", *, code="erasure_refused", **extra):
        super().__init__(message)
        self.code = code
        self.extra = extra


def register(source: PersonalDataSource) -> PersonalDataSource:
    registry[source.model] = source
    return source


# ---------------------------------------------------------------- Art. 15: access
#: Rows one page reads, deletes or anonymises, for export and erasure alike
#: (`PRIVACY_BATCH_ROWS`). What a page holds — the rows as dicts, or the instances
#: Django collects to delete them — is bounded by this, never by how long the person
#: has used the app: 180 days of activity log, years of set logs and messages.
DEFAULT_BATCH_ROWS = 1000


def batch_rows() -> int:
    from django.conf import settings

    return max(1, int(getattr(settings, "PRIVACY_BATCH_ROWS", DEFAULT_BATCH_ROWS)))


def _export_columns(source, model) -> list[str]:
    # Concrete COLUMNS only. `ManyToManyField.concrete` is True, so this list used to
    # include every M2M on the model and `.values()` turned each one into a LEFT JOIN:
    # `diet.UserFoodPreference` has seven, and one client with eight likes, eight
    # dislikes and four foods in each of five choice lists produced 8 x 8 x 4^5 =
    # 65,536 rows for a single record. The request hung, the broad handler filed the
    # section as an error, and the person received a silently incomplete export of
    # their own data.
    return source.fields or [
        f.name for f in model._meta.get_fields()
        if getattr(f, "concrete", False) and not f.many_to_many
    ]


def section_pages(source, user, *, size: int | None = None):
    """Yield one registered source's rows for `user`, a page (a list of dicts) at a time,
    in the section's own order (the model's default ordering, as before).

    Only the section's primary keys are held whole — a few bytes a row, in order —
    and `size` rows at a time are read as dicts, with their many-to-many ids attached
    by one query per relation for the page. The whole section used to be one
    `list(qs.values(...))`, and then every row again as a model instance to read its
    many-to-many ids one query per row (the production-readiness audit, 2026-09-25).
    """
    size = size or batch_rows()
    model = source.get_model()
    names = _export_columns(source, model)
    # Once each, in order: an ordering through a join can repeat a row's key.
    ids = list(dict.fromkeys(source.queryset_for(user).values_list("pk", flat=True)))
    links = [f for f in model._meta.get_fields()
             if f.many_to_many and getattr(f, "concrete", False)]
    manager = model._default_manager
    for start in range(0, len(ids), size):
        page_ids = ids[start:start + size]
        by_pk = {}
        for row in manager.filter(pk__in=page_ids).values("pk", *names):
            key = row["pk"]
            if "pk" not in names:
                del row["pk"]
            by_pk[key] = row
        # The M2M contents are still theirs: attached as lists of ids, in the related
        # model's own order, as the per-object manager read them.
        for link in links:
            found: dict = {}
            back = link.related_query_name()
            if back.endswith("+"):
                # No reverse name to join through: the through table, in its own order.
                through = link.remote_field.through
                source_column = through._meta.get_field(link.m2m_field_name()).attname
                target_column = through._meta.get_field(link.m2m_reverse_field_name()).attname
                pairs = (through._default_manager
                         .filter(**{f"{source_column}__in": page_ids})
                         .order_by("pk").values_list(source_column, target_column))
            else:
                pairs = (link.related_model._default_manager
                         .filter(**{f"{back}__in": page_ids})
                         .values_list(back, "pk"))
            for owner, target in pairs:
                found.setdefault(owner, []).append(target)
            for key, row in by_pk.items():
                row[link.name] = found.get(key, [])
        yield [by_pk[key] for key in page_ids if key in by_pk]


def export_user_data(user) -> dict:
    """Everything the platform holds about `user`, as JSON-serialisable data.

    The whole export in memory, for code that wants it as a dict. The route writes it
    a page at a time instead (`write_user_data`)."""
    from django.utils import timezone

    out = {
        "generated_at": timezone.now().isoformat(),
        "user_id": user.pk,
        "sections": {},
    }
    for source in registry.values():
        try:
            rows = []
            for page in section_pages(source, user):
                rows.extend(page)
            out["sections"][source.label] = rows
        except Exception:
            # One unexportable model must not deny the whole request.
            logger.exception("Export failed for %s", source.model)
            out["sections"][source.label] = {"error": "could not be exported"}
    return out


def write_user_data(user, out) -> None:
    """`export_user_data`, written to the binary file `out` as JSON, a page at a time.

    The same document — `{generated_at, user_id, sections: {<label>: [rows]}}`, a
    section that cannot be exported answering `{"error": "could not be exported"}` —
    but never more than one page of rows in memory: the export route spools this to
    disk past `PRIVACY_EXPORT_SPOOL_BYTES` and streams it from there
    (`privacy.views.PersonalDataExportView`). A section that fails part-way is cut back
    to where it began, so the file is always valid JSON.
    """
    import json

    from django.core.serializers.json import DjangoJSONEncoder
    from django.utils import timezone

    def dump(value) -> bytes:
        return json.dumps(value, cls=DjangoJSONEncoder).encode("utf-8")

    out.write(b'{"generated_at": ' + dump(timezone.now().isoformat())
              + b', "user_id": ' + dump(user.pk) + b', "sections": {')
    for index, source in enumerate(registry.values()):
        if index:
            out.write(b", ")
        out.write(dump(source.label) + b": ")
        began = out.tell()
        try:
            out.write(b"[")
            first = True
            for page in section_pages(source, user):
                for row in page:
                    out.write((b"" if first else b", ") + dump(row))
                    first = False
            out.write(b"]")
        except Exception:
            # One unexportable model must not deny the whole request.
            logger.exception("Export failed for %s", source.model)
            out.seek(began)
            out.truncate()
            out.write(dump({"error": "could not be exported"}))
    out.write(b"}}")


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
                if action == "delete" and source.delete_rows is not None:
                    source.delete_rows(user, qs)
                elif action == "delete":
                    # A page of primary keys per DELETE: a model with cascades, a
                    # SET_NULL pointing at it or a delete receiver is loaded row by row
                    # to be deleted, and one `qs.delete()` loaded all of a long-time
                    # user's rows at once (`_delete_in_batches`).
                    _delete_in_batches(qs, batch=batch_rows())
                elif action == "anonymise":
                    values = {
                        k: (v(user) if callable(v) else v)
                        for k, v in source.anonymise.items()
                    }
                    # save() per row, not queryset.update(): update() skips every signal,
                    # so clearing an ImageField would leave the file on disk forever —
                    # the same defect found in the admin's bulk actions. A page of rows
                    # at a time, walked by primary key.
                    last = None
                    while True:
                        page = qs.order_by("pk")
                        if last is not None:
                            page = page.filter(pk__gt=last)
                        rows = list(page[:batch_rows()])
                        if not rows:
                            break
                        for obj in rows:
                            for key, value in values.items():
                                setattr(obj, key, value)
                            obj.save(update_fields=list(values))
                        last = rows[-1].pk
            if source.erase_also is not None and not dry_run:
                # Not counted: see `PersonalDataSource.erase_also`.
                _delete_in_batches(source.erase_also(user), batch=batch_rows())
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


def _delete_in_batches(queryset, batch: int = PURGE_BATCH) -> int:
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
        # `order_by()`: which rows go first does not matter, and an ordering column is
        # one more thing a DISTINCT (every erasure queryset has one) must select.
        ids = list(queryset.order_by().values_list("pk", flat=True)[:batch])
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
        if source.erase_also is not None and source.on_erase != "delete":
            problems.append(
                f"{source.label}: erase_also deletes rows, so it needs on_erase='delete', "
                f"not {source.on_erase!r}"
            )
    return problems


# ------------------------------------------------------------------- coverage
def audit_coverage() -> list[str]:
    """Models holding a user FK that no source claims.

    This is what stops the registry rotting: a model added later that nobody registered
    shows up here instead of being quietly excluded from export, erasure and retention.
    """
    import site
    import sysconfig
    from pathlib import Path

    from django.apps import apps
    from django.conf import settings
    from django.db import models as M

    # Every first-party app whose models the audit walks — DERIVED, not typed out. The
    # list used to be a literal, and `mediakit` was never added to it: a model there
    # holding a user FK would have been silently exempt from the "every personal-data
    # model is registered" gate, which is precisely the rot this function exists to
    # catch, and `MediaAsset` is registered only because someone remembered. An app
    # whose code lives inside this repository is ours and is walked, so a new app is
    # covered by the act of adding it to INSTALLED_APPS; a third-party package lives
    # in site-packages and is not.
    # "Inside the repository" is not enough on its own: the virtualenv lives at
    # `<repo>/.venv`, so every installed package is also under BASE_DIR and allauth,
    # DRF's authtoken and simplejwt's blacklist all read as ours. An app is ours when
    # it is under the repository and NOT under any site-packages directory.
    root = str(Path(settings.BASE_DIR).resolve())
    vendored = tuple(str(Path(path).resolve()) + "/"
                     for path in {*sysconfig.get_paths().values(), *site.getsitepackages(),
                                  site.getusersitepackages()} if path)
    local = set()
    for config in apps.get_app_configs():
        path = str(Path(config.path).resolve())
        if path.startswith(root + "/") and not path.startswith(vendored):
            local.add(config.label)
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

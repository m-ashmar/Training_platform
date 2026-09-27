"""The data inventory, written from the privacy registry (spec §9 B3, plan step 12).

    python manage.py data_inventory             # writes docs/store/data_inventory.md
    python manage.py data_inventory --check     # exit 1 when the committed file is stale
    python manage.py data_inventory --output /tmp/inventory.md

Spec §9 B3 asks for "a data inventory ... listing every field collected, why, and who
receives it. It feeds the privacy policy, Apple's privacy labels and Play's Data safety
form, which must all agree." The registry (`training_platform/privacy/sources.py`)
already lists every model holding personal data — the coverage gate fails when one is
missing — and each source declares there what it holds, why (`purpose`), who else
receives it (`recipients`) and what kind of data it is on the stores' forms
(`data_types`). This command only writes that down. It adds no knowledge of its own, so
the inventory cannot say something the registry does not.

For each source: the model and how it reaches the person, why it is held, who receives
it, its data types, every field the model stores, what the export
(`GET /api/privacy/export/`) includes, what an erasure does to it, and how long it is
kept. Then the tables the store forms are filled from: recipients, data types and a
one-line summary per source.

**Refuses** (and writes nothing) while the registry cannot answer truthfully: a source
without a purpose, recipients or data types, a key the vocabulary
(`training_platform/privacy/disclosure.py`) does not know, a model holding personal data
that no source claims (`privacy.audit_coverage`), or a source that names a field its
model does not have (`privacy.validate_sources`).

The output is the same bytes for the same registry — no date, no host — so
`tests/test_store_docs.py` can hold the committed file to the registry: a new model, a
new field or a changed purpose fails that test until the command is run again.
"""
from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

#: Where the inventory lives, relative to the repository.
DEFAULT_OUTPUT = "docs/store/data_inventory.md"

ERASURE = {
    "delete": "deleted",
    "anonymise": "anonymised",
    "keep": "kept",
}


# ------------------------------------------------------------------ checking
def problems() -> list[str]:
    """Why the registry cannot be written down truthfully; empty when it can."""
    from training_platform.privacy import audit_coverage, registry, validate_sources
    from training_platform.privacy.disclosure import DATA_TYPES, RECIPIENTS

    found = []
    for source in registry.values():
        if not (source.purpose or "").strip():
            found.append(f"{source.label}: no purpose")
        if source.recipients is None:
            found.append(f"{source.label}: recipients undeclared (() means nobody)")
        else:
            found += [f"{source.label}: unknown recipient {key!r}"
                      for key in source.recipients if key not in RECIPIENTS]
        if not source.data_types:
            found.append(f"{source.label}: no data types")
        else:
            found += [f"{source.label}: unknown data type {key!r}"
                      for key in source.data_types if key not in DATA_TYPES]
    found += [f"{model}: holds personal data and no source claims it"
              for model in audit_coverage()]
    found += validate_sources()
    return found


# ------------------------------------------------------------------ reading
def _paths(source) -> tuple[str, ...]:
    return ((source.user_field,) if isinstance(source.user_field, str)
            else tuple(source.user_field))


def _stored_fields(model) -> list[str]:
    return [f.name for f in model._meta.get_fields() if getattr(f, "concrete", False)]


def _exported_fields(source, model) -> list[str]:
    """What `registry.section_pages` writes for one row: the export's columns, plus every
    many-to-many as a list of ids."""
    from training_platform.privacy.registry import _export_columns

    names = list(_export_columns(source, model))
    names += [f.name for f in model._meta.get_fields()
              if f.many_to_many and getattr(f, "concrete", False) and f.name not in names]
    return names


def _retention(source) -> str:
    if not source.retention_days:
        return ""
    if source.retention_is_deadline:
        return (f"deleted when `{source.retention_field}` passes (set at most "
                f"{source.retention_days} days after the row is written)")
    return f"{source.retention_days} days after `{source.retention_field}`"


def _code(names) -> str:
    return ", ".join(f"`{name}`" for name in names)


def _cell(text: str) -> str:
    """Markdown table cells cannot hold a bare `|`."""
    return text.replace("|", "/")


# ------------------------------------------------------------------ writing
def render() -> str:
    from training_platform.privacy import registry
    from training_platform.privacy.disclosure import DATA_TYPES, PROCESSORS, RECIPIENTS

    sources = list(registry.values())
    out: list[str] = []
    w = out.append

    w("# Data inventory")
    w("")
    w("> **Generated** by `python manage.py data_inventory` from the privacy registry "
      "(`training_platform/privacy/sources.py`; the words in "
      "`training_platform/privacy/disclosure.py`). Do not edit it by hand: change the "
      "registry, run the command, commit both. `tests/test_store_docs.py` fails while "
      "this file and the registry disagree.")
    w("")
    w("What the platform's server holds about a person, why, who else receives it, what "
      "the person can download and what happens when they delete their account "
      "(spec §9 B3). `privacy_labels.md` (Apple's App Privacy answers and Google's Data "
      "safety answers) and the privacy-policy drafts in `docs/legal/` are written from "
      "it, and must list the same data.")
    w("")
    w(f"{len(sources)} sources, in the order they are registered.")
    w("")
    w("## What every source has in common")
    w("")
    w("- **Download.** Every source is a section of the person's export, "
      "`GET /api/privacy/export/` (a JSON file), with the fields listed as exported below.")
    w("- **Deletion.** In the app: `DELETE /api/privacy/erase/?confirm=ERASE` (`GET` "
      "previews it). Without the app: a request on the web page `/legal/delete-account/`, "
      "which an operator confirms with the owner of the address and carries out through "
      "the same erasure. Each source says what erasure does to it: **deleted**; "
      "**anonymised** (the fields named are cleared or replaced, the others stay on a row "
      "that no longer names anyone); or **kept** (a record of money, or content other "
      "people's programs are built on). The account row itself is anonymised and "
      "deactivated.")
    w("- **Retention.** A source with a window is purged every night "
      "(`training_platform.privacy.purge_expired_personal_data`); the others are kept "
      "while the account exists.")
    w("- **Operators.** The platform's own operators see records in the console as their "
      "work needs. They are the platform, not a recipient.")
    w("- **In transit.** Encrypted: production redirects HTTP to HTTPS and sends HSTS "
      "(`settings_production.py`, `fly.toml force_https`).")
    w("")
    w("## Service providers that hold every source")
    w("")
    w("| Provider | What it does |")
    w("|---|---|")
    for processor in PROCESSORS:
        w(f"| {_cell(processor.name)} | {_cell(processor.what)} |")
    w("")
    w("## Recipients")
    w("")
    w("Who, besides those providers and the platform's operators, receives data, and "
      "which sources reach them.")
    w("")
    w("| Key | Who | Kind | What they receive | Sources |")
    w("|---|---|---|---|---|")
    for key, recipient in RECIPIENTS.items():
        using = [s.label for s in sources if key in (s.recipients or ())]
        w(f"| `{key}` | {_cell(recipient.name)} | {recipient.kind} | "
          f"{_cell(recipient.receives)} | {_code(using) or 'none'} |")
    w("")
    w("## Data types, as the stores name them")
    w("")
    w("| Key | In plain words | Apple App Privacy | Google Play Data safety | Sources |")
    w("|---|---|---|---|---|")
    for key, kind in DATA_TYPES.items():
        using = [s.label for s in sources if key in (s.data_types or ())]
        apple = kind.apple or f"not declared: {kind.not_declared}"
        google = kind.google or "not declared"
        w(f"| `{key}` | {_cell(kind.name)} | {_cell(apple)} | {_cell(google)} | "
          f"{_code(using) or 'none'} |")
    w("")
    w("## Summary")
    w("")
    w("| Source | Model | Data types | Recipients | Erasure | Retention |")
    w("|---|---|---|---|---|---|")
    for source in sources:
        retention = (f"{source.retention_days} days" if source.retention_days else "-")
        w(f"| `{source.label}` | `{source.model}` | {_code(source.data_types or ())} | "
          f"{_code(source.recipients or ()) or 'nobody'} | {ERASURE[source.on_erase]} | "
          f"{retention} |")
    w("")
    w("## Sources")
    for source in sources:
        model = source.get_model()
        stored = _stored_fields(model)
        exported = _exported_fields(source, model)
        w("")
        w(f"### `{source.label}` — `{source.model}`")
        w("")
        w(f"**Why.** {source.purpose.strip()}")
        w("")
        paths = _paths(source)
        if paths in (("pk",), ("id",)):
            w("**Whose.** The account's own row.")
        else:
            via = " or ".join(f"`{path}`" for path in paths)
            how = (f"`{source.user_value.__name__}(account)`, derived on the server"
                   if source.user_value is not None else "the account")
            w(f"**Whose.** Rows whose {via} is {how}.")
        w("")
        if source.recipients:
            names = "; ".join(f"{RECIPIENTS[key].name} (`{key}`)"
                              for key in source.recipients)
            w(f"**Who else receives it.** {names}.")
        else:
            w("**Who else receives it.** Nobody outside the platform.")
        w("")
        w(f"**Data types.** " + "; ".join(
            f"{DATA_TYPES[key].name} (`{key}`)" for key in source.data_types) + ".")
        w("")
        w(f"**Fields stored.** {_code(stored)}.")
        w("")
        left_out = [name for name in stored if name not in exported]
        if left_out:
            w(f"**In the export.** {_code(exported)}. Not exported: {_code(left_out)}.")
        else:
            w("**In the export.** Every field.")
        w("")
        if source.on_erase == "anonymise":
            cleared = list(source.anonymise)
            kept = [name for name in stored if name not in cleared and name != "id"]
            w(f"**On erasure.** Anonymised: {_code(cleared)} are cleared or replaced; "
              f"{_code(kept)} stay on the row.")
        elif source.on_erase == "keep":
            w("**On erasure.** Kept.")
        else:
            w("**On erasure.** Deleted.")
        extra = []
        if source.before_erase is not None:
            extra.append(f"before any row goes, `{source.before_erase.__name__}` runs "
                         f"(`sources.py`)")
        if source.delete_rows is not None:
            extra.append(f"the rows are deleted by `{source.delete_rows.__name__}`, files "
                         f"first")
        if source.erase_also is not None:
            extra.append(f"`{source.erase_also.__name__}` also deletes rows about the "
                         f"person that other people hold, which are not exported")
        if extra:
            w("")
            w("**Also on erasure.** " + "; ".join(extra) + ".")
        w("")
        retention = _retention(source)
        if retention:
            w(f"**Retention.** Purged nightly: {retention}.")
        elif source.on_erase == "keep":
            w("**Retention.** Kept for good, and after erasure.")
        else:
            w("**Retention.** While the account exists.")
    w("")
    return "\n".join(out)


class Command(BaseCommand):
    help = ("Write the data inventory (docs/store/data_inventory.md) from the privacy "
            "registry, or --check that the committed one is current.")

    def add_arguments(self, parser):
        parser.add_argument(
            "--output", default=DEFAULT_OUTPUT,
            help=f"Where to write it, relative to the repository (default {DEFAULT_OUTPUT}).")
        parser.add_argument(
            "--check", action="store_true",
            help="Write nothing; fail when the file differs from what the registry says.")

    def handle(self, *args, **options):
        found = problems()
        if found:
            raise CommandError(
                "Refusing to write the inventory: the registry cannot answer for these "
                "sources yet (training_platform/privacy/sources.py):\n  "
                + "\n  ".join(found))
        text = render()
        path = Path(options["output"])
        if not path.is_absolute():
            path = Path(settings.BASE_DIR) / path
        if options["check"]:
            current = path.read_text(encoding="utf-8") if path.exists() else None
            if current != text:
                raise CommandError(
                    f"{path} is out of date with the privacy registry. Run "
                    f"`python manage.py data_inventory` and commit the result.")
            self.stdout.write(self.style.SUCCESS(f"{path} is current."))
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        self.stdout.write(self.style.SUCCESS(
            f"wrote {path} ({text.count(chr(10)) + 1} lines)"))

"""The store review documents (plan step 12; spec §9 B3, B7, B8 and §10).

Spec §11's last acceptance line: "The privacy policy, Apple privacy labels and Play Data
safety form list the same data." These tests hold the documents the owner submits to
the code they describe:

  * `docs/store/data_inventory.md` is `manage.py data_inventory`'s output, byte for byte,
    so a model registered in the privacy registry — or a field added to one, or a
    purpose changed — fails here until the inventory is regenerated and committed;
  * every source declares why it is held, who else receives it and what kind of data it
    is on the stores' forms, in words the vocabulary knows, or the command refuses;
  * every data type the inventory declares is answered in `privacy_labels.md` (Apple and
    Google) and named in both privacy-policy drafts;
  * the build table in `review_notes.md` is `policy/defaults.py`, and its account table
    is `seed_review_accounts`;
  * the legal drafts say DRAFT, and carry no price and no link but the legal pages'.

None of them needs the database: the registry and the model metadata are enough.
"""
import re
from pathlib import Path

import pytest
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError

ROOT = Path(settings.BASE_DIR)
INVENTORY = ROOT / "docs" / "store" / "data_inventory.md"
LABELS = ROOT / "docs" / "store" / "privacy_labels.md"
REVIEW_NOTES = ROOT / "docs" / "store" / "review_notes.md"
LEGAL = ROOT / "docs" / "legal"
DRAFTS = ("privacy_policy_en.md", "privacy_policy_ar.md", "terms_en.md", "terms_ar.md")
BODY_LINE = "===== BODY:"


def _read(path):
    return path.read_text(encoding="utf-8")


def _body(name):
    """What would be pasted into the console: everything below the BODY line."""
    text = _read(LEGAL / name)
    assert BODY_LINE in text, f"{name} has no BODY line"
    return text.split(BODY_LINE, 1)[1].split("\n", 1)[1]


def _declared_types():
    """The data types some registered source declares and at least one store form asks
    about."""
    from training_platform.privacy import registry
    from training_platform.privacy.disclosure import DATA_TYPES

    used = {key for source in registry.values() for key in (source.data_types or ())}
    return {key: DATA_TYPES[key] for key in sorted(used)
            if DATA_TYPES[key].apple or DATA_TYPES[key].google}


def _form_entries(label):
    """"Photos and videos > Photos, Videos" -> the two lines a form shows."""
    category, _sep, types = label.partition(" > ")
    return [f"{category} > {kind.strip()}" for kind in types.split(",")]


@pytest.fixture
def declared_source():
    """A new model's source, fully declared — as a registration added tomorrow would be."""
    from training_platform.privacy.registry import PersonalDataSource

    return PersonalDataSource(
        label="a_new_source", model="users.DeviceToken", user_field="user",
        purpose="Something a later build starts to keep.", recipients=(),
        data_types=("app_activity",))


# ------------------------------------------------------------------ the registry
def test_every_registered_source_declares_why_who_and_what():
    """The inventory can only say what the registry says. Every source names its
    purpose, its recipients (`()` for nobody) and its data types, in the vocabulary's
    words; no model holding personal data is unclaimed; no source names a missing
    field."""
    from legal.management.commands.data_inventory import problems

    assert problems() == []


def test_an_undeclared_source_stops_the_command(monkeypatch, tmp_path):
    from training_platform.privacy import registry
    from training_platform.privacy.registry import PersonalDataSource

    monkeypatch.setitem(registry, "tests.Undeclared", PersonalDataSource(
        label="undeclared", model="users.DeviceToken", user_field="user"))
    monkeypatch.setitem(registry, "tests.Unknown", PersonalDataSource(
        label="unknown_words", model="users.DeviceToken", user_field="user",
        purpose="x", recipients=("the_neighbours",), data_types=("horoscope",)))
    out = tmp_path / "inventory.md"

    with pytest.raises(CommandError) as refused:
        call_command("data_inventory", "--output", str(out))

    message = str(refused.value)
    assert "undeclared: no purpose" in message
    assert "undeclared: recipients undeclared" in message
    assert "undeclared: no data types" in message
    assert "unknown_words: unknown recipient 'the_neighbours'" in message
    assert "unknown_words: unknown data type 'horoscope'" in message
    assert not out.exists(), "a refused run must write nothing"


# ------------------------------------------------------------------ the inventory
def test_the_inventory_covers_every_registered_source(tmp_path):
    from training_platform.privacy import registry

    out = tmp_path / "inventory.md"
    call_command("data_inventory", "--output", str(out))
    text = _read(out)

    headings = re.findall(r"^### `([^`]+)` — `([^`]+)`$", text, flags=re.M)
    assert headings == [(source.label, source.model) for source in registry.values()]
    for source in registry.values():
        section = text.split(f"### `{source.label}` — `{source.model}`", 1)[1]
        section = section.split("\n### ", 1)[0]
        assert source.purpose.strip() in section
        for name in (f.name for f in source.get_model()._meta.get_fields()
                     if getattr(f, "concrete", False)):
            assert f"`{name}`" in section, f"{source.label}: field {name} not listed"
        assert "**On erasure.**" in section and "**Retention.**" in section
        assert "**In the export.**" in section


def test_the_committed_inventory_is_the_registry_written_down():
    """A new model, a new field or a changed purpose fails here until
    `python manage.py data_inventory` is run and its output committed."""
    from legal.management.commands.data_inventory import render

    assert INVENTORY.exists(), "docs/store/data_inventory.md is missing"
    assert _read(INVENTORY) == render(), (
        "docs/store/data_inventory.md is out of date with the privacy registry: run "
        "`python manage.py data_inventory` and commit the result")


def test_a_new_source_makes_the_committed_inventory_stale(monkeypatch, declared_source):
    from legal.management.commands.data_inventory import render
    from training_platform.privacy import registry

    monkeypatch.setitem(registry, "tests.NewSource", declared_source)

    fresh = render()
    assert "### `a_new_source` — `users.DeviceToken`" in fresh
    assert fresh != _read(INVENTORY)
    with pytest.raises(CommandError, match="out of date"):
        call_command("data_inventory", "--check")


def test_check_passes_on_the_committed_file_and_fails_on_a_stale_one(tmp_path):
    call_command("data_inventory", "--check")  # the committed file

    stale = tmp_path / "inventory.md"
    stale.write_text("# Data inventory\n", encoding="utf-8")
    with pytest.raises(CommandError, match="out of date"):
        call_command("data_inventory", "--check", "--output", str(stale))
    with pytest.raises(CommandError, match="out of date"):
        call_command("data_inventory", "--check", "--output", str(tmp_path / "none.md"))


def test_the_inventory_says_what_erasure_leaves_on_an_anonymised_row():
    """The profile is anonymised, not deleted: the inventory names the fields cleared
    AND the fields that stay, so no document can claim more than the erasure does."""
    from legal.management.commands.data_inventory import render
    from training_platform.privacy import registry

    profile = registry["users.CustomUser"]
    section = render().split("### `profile` — `users.CustomUser`", 1)[1].split("\n### ")[0]
    line = next(row for row in section.splitlines() if row.startswith("**On erasure.**"))
    cleared, stays = line.split("are cleared or replaced;")
    cleared = set(re.findall(r"`([^`]+)`", cleared))
    stays = set(re.findall(r"`([^`]+)`", stays))
    assert cleared == set(profile.anonymise)
    stored = {f.name for f in profile.get_model()._meta.get_fields()
              if getattr(f, "concrete", False)}
    assert cleared | stays == stored - {"id"} and not cleared & stays


# ------------------------------------------------------------ the three must agree
def test_privacy_labels_answer_every_declared_data_type():
    labels = _read(LABELS)
    missing = []
    for key, kind in _declared_types().items():
        for form in (kind.apple, kind.google):
            for entry in _form_entries(form) if form else ():
                if entry not in labels:
                    missing.append(f"{key}: {entry}")
    assert missing == [], "privacy_labels.md does not answer: " + ", ".join(missing)


@pytest.mark.parametrize("name, attribute", [("privacy_policy_en.md", "name"),
                                             ("privacy_policy_ar.md", "name_ar")])
def test_the_privacy_policy_names_every_declared_data_type(name, attribute):
    body = _body(name)
    missing = [key for key, kind in _declared_types().items()
               if getattr(kind, attribute) not in body]
    assert missing == [], f"{name} does not name: {', '.join(missing)}"


# ------------------------------------------------------------------ the legal drafts
@pytest.mark.parametrize("name", DRAFTS)
def test_the_legal_drafts_say_draft_and_carry_no_price_and_no_payment_link(name):
    text = _read(LEGAL / name)
    assert "DRAFT" in text.splitlines()[0], f"{name}: the first line must say DRAFT"
    body = _body(name)
    # No amount of money: every build opens these pages, the store builds included.
    assert not re.search(r"SYP|USD|\$|€|ل\.س|ليرة|دولار", body), name
    assert not re.search(r"\d[\d.,]*\s*(%|٪)", body), f"{name}: a rate or a percentage"
    # No link but the legal pages' own.
    for url in re.findall(r"https?://\S+", body):
        assert "/legal/" in url, f"{name}: {url}"
    assert not re.search(r"checkout|hub|subscribe on|اشترك عبر", body, flags=re.I), name


def test_the_publishing_note_exists_and_names_the_console():
    note = _read(LEGAL / "README.md")
    assert "/dj-admin/legal/legaldocument/" in note
    assert "Start a new draft from this version" in note


# ------------------------------------------------------------------ the review notes
def test_the_review_notes_build_table_is_the_policy_default():
    from policy.defaults import DEFAULT_FLAGS

    rows = {}
    for line in _read(REVIEW_NOTES).splitlines():
        match = re.match(r"^\| `(can_[a-z_]+)` \| (.+) \|$", line)
        if match:
            rows[match.group(1)] = [cell.strip() for cell in match.group(2).split("|")]
    channels = ("apk", "play", "ios", "web")
    expected = {flag: ["yes" if DEFAULT_FLAGS[channel][flag] else "no"
                       for channel in channels]
                for flag in DEFAULT_FLAGS["apk"]}
    assert rows == expected


def test_the_review_notes_list_every_review_account_and_no_password():
    from users.management.commands.seed_review_accounts import PASSWORD_ENV, PEOPLE

    notes = _read(REVIEW_NOTES)
    for role in PEOPLE:
        assert re.search(rf"^\| `{role}` \|", notes, flags=re.M), role
    assert "seed_review_accounts" in notes
    for path in (REVIEW_NOTES, LABELS, INVENTORY, *(LEGAL / name for name in DRAFTS)):
        text = _read(path)
        for value in re.findall(rf"{PASSWORD_ENV}='([^']*)'", text):
            assert value.startswith("<"), f"{path.name}: a password is written down"
        assert "Yalla!Gym2026" not in text

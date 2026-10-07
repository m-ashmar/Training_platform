"""Repair of wave 1 (the owner's fix run), the privacy finding of the adversarial review.

* R-2 (SIDE_BUGS S-233's command) — `manage.py reencrypt_fields` read a page of raw
  values without a lock and then wrote each row back by primary key alone. A person who
  erased their account, edited their injury or replaced their payer address after the
  page was read and before the command reached their row had that write undone: the
  re-encrypted OLD value went back on the row. An erased injury came back on the
  anonymised account; an address reverted while its digest described the new one.

Each test runs the other write at the exact moment the command reaches that row, which
is the interleaving a concurrent request produces: `MultiFernet.rotate` is wrapped so
that, when it is handed that row's stored token, the person's write lands first. Every
test fails on the code before the fix (39ae624).
"""
import io

import pytest
from cryptography.fernet import Fernet, MultiFernet
from django.core.management import CommandError, call_command


def _raw(model, pk, column):
    """The stored text of an encrypted column, without the field's own decryption."""
    from django.db import models
    from django.db.models.functions import Cast

    return (model._base_manager.filter(pk=pk)
            .annotate(raw=Cast(column, output_field=models.TextField()))
            .values_list("raw", flat=True).get())


def _when_rotating(monkeypatch, token, write):
    """Run `write()` once, just before the command rotates the stored `token`: the
    person's request landing between the command's read of the page and its write of
    this row."""
    original = MultiFernet.rotate
    fired = []

    def rotate(self, msg):
        if msg == token.encode("utf-8") and not fired:
            fired.append(True)
            write()
        return original(self, msg)

    monkeypatch.setattr(MultiFernet, "rotate", rotate)
    return fired


def _keys(settings):
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    settings.FIELD_ENCRYPTION_KEY = old
    return old, new


@pytest.mark.django_db
def test_r2_an_erasure_in_the_window_is_not_undone_by_the_rotation(
        make_user, settings, monkeypatch):
    """The review's probe: erasure ran while the command walked the users, and the
    injury text was written back onto the anonymised, inactive row — readable by the
    console, and never cleared again."""
    from training_platform.privacy.registry import erase_user_data
    from users.models import CustomUser

    old, new = _keys(settings)
    person = make_user("w1r_r2_erase", specific_injury="torn ACL left knee",
                       injury_areas=["knee"])
    settings.FIELD_ENCRYPTION_KEY = f"{new},{old}"
    fired = _when_rotating(monkeypatch, _raw(CustomUser, person.pk, "specific_injury"),
                           lambda: erase_user_data(CustomUser.objects.get(pk=person.pk)))

    out = io.StringIO()
    call_command("reencrypt_fields", stdout=out)

    assert fired, "the command never reached the person's row"
    erased = CustomUser.objects.get(pk=person.pk)
    assert erased.is_active is False
    assert _raw(CustomUser, person.pk, "specific_injury") == "", \
        "the erased injury came back on the anonymised row"
    assert erased.specific_injury == ""
    assert erased.injury_areas == []
    assert "users.CustomUser.specific_injury: 0 re-encrypted" in out.getvalue(), \
        out.getvalue()
    assert "1 changed while this ran" in out.getvalue(), out.getvalue()


@pytest.mark.django_db
@pytest.mark.parametrize("writer", ["the_app", "a_process_still_on_the_old_key"])
def test_r2_an_edit_in_the_window_is_kept_and_still_ends_under_the_first_key(
        make_user, settings, monkeypatch, writer):
    """The person's edit wins over the command's stale read, and the rotation still
    finishes: the newer value is read again and rewritten under the first key, so the
    old key can be dropped — even when the edit came from a process that had not yet
    been given the new key."""
    from django.db import models

    from users.models import CustomUser

    old, new = _keys(settings)
    person = make_user("w1r_r2_edit", specific_injury="torn ACL left knee")
    settings.FIELD_ENCRYPTION_KEY = f"{new},{old}"

    def edit():
        if writer == "the_app":
            row = CustomUser.objects.get(pk=person.pk)
            row.specific_injury = "healed, cleared to train"
            row.save(update_fields=["specific_injury"])
        else:
            token = Fernet(old.encode()).encrypt(b"healed, cleared to train").decode()
            CustomUser.objects.filter(pk=person.pk).update(
                specific_injury=models.Value(token, output_field=models.TextField()))

    fired = _when_rotating(monkeypatch, _raw(CustomUser, person.pk, "specific_injury"),
                           edit)
    out = io.StringIO()
    call_command("reencrypt_fields", stdout=out)
    assert fired, "the command never reached the person's row"

    settings.FIELD_ENCRYPTION_KEY = new  # the old key is dropped
    assert CustomUser.objects.get(pk=person.pk).specific_injury == \
        "healed, cleared to train"
    assert Fernet(new.encode()).decrypt(
        _raw(CustomUser, person.pk, "specific_injury").encode()) == \
        b"healed, cleared to train"
    assert ("users.CustomUser.specific_injury: 1 re-encrypted, 0 plain text encrypted, "
            "0 under an unknown key, 1 changed while this ran") in out.getvalue(), \
        out.getvalue()


@pytest.mark.django_db
def test_r2_a_payer_address_replaced_in_the_window_keeps_the_new_address(
        make_user, settings, monkeypatch):
    """`payer_address.save` edits the row in place. The blind write reverted the
    address while `address_digest` and `verified_at` still described the new one, so
    the second matching signal pointed at an address the row no longer held."""
    from subscription import payer_address
    from subscription.models import PayerAddress

    old, new = _keys(settings)
    person = make_user("w1r_r2_payer")
    row = payer_address.save(person, "0912345678", "0912345678")
    settings.FIELD_ENCRYPTION_KEY = f"{new},{old}"

    fired = _when_rotating(monkeypatch, _raw(PayerAddress, row.pk, "address"),
                           lambda: payer_address.save(person, "abcd1234efgh", "abcd1234efgh"))
    call_command("reencrypt_fields", stdout=io.StringIO())
    assert fired, "the command never reached the payer address"

    settings.FIELD_ENCRYPTION_KEY = new  # the old key is dropped
    row = PayerAddress.objects.get(pk=row.pk)
    assert row.address == "abcd1234efgh"
    assert row.address_digest == payer_address.address_digest("abcd1234efgh")


@pytest.mark.django_db
def test_r2_a_value_that_changes_on_every_read_fails_the_run_instead_of_being_overwritten(
        make_user, settings, monkeypatch):
    """A row the command could not pin down is not overwritten with a stale value and
    not reported as done: the run exits non-zero naming it, as it does for a value under
    an unknown key, and the person's last write is what the row holds."""
    from users.models import CustomUser

    old, new = _keys(settings)
    person = make_user("w1r_r2_busy", specific_injury="busy-0")
    settings.FIELD_ENCRYPTION_KEY = f"{new},{old}"
    original = MultiFernet.rotate
    writes = []

    def rotate(self, msg):
        if self.decrypt(msg).startswith(b"busy-"):
            writes.append(True)
            row = CustomUser.objects.get(pk=person.pk)
            row.specific_injury = f"busy-{len(writes)}"
            row.save(update_fields=["specific_injury"])
        return original(self, msg)

    monkeypatch.setattr(MultiFernet, "rotate", rotate)
    with pytest.raises(CommandError,
                       match=rf"changed every time.*users.CustomUser.specific_injury "
                             rf"pk={person.pk}"):
        call_command("reencrypt_fields", stdout=io.StringIO())

    settings.FIELD_ENCRYPTION_KEY = new
    assert CustomUser.objects.get(pk=person.pk).specific_injury == f"busy-{len(writes)}"

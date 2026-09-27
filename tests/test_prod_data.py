"""Data safety and heavy work in requests (the production-readiness audit, 2026-09-25).

1. The ledger (`wallet_transaction`) is append-only AT THE DATABASE, like the audit log,
   the agent receivable and a withdrawal's events already were.
2. Account erasure and data export hold one page of a person's rows at a time, and the
   stored files of what was erased leave storage after the commit, off the request,
   through a task that retries what storage refuses.
3. One process decodes at most as many images at once as its memory holds at the
   decode budget; an upload that cannot get a slot in time is answered `upload_busy`
   (503, `Retry-After`) and is not decoded.
4. Periodic jobs over every user take one page per invocation and hand the rest on to a
   fresh invocation of themselves, so a large user base cannot time a job out halfway.
"""
import io
import json
import threading
import tracemalloc
import uuid
from contextlib import contextmanager
from datetime import timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal as D
from types import SimpleNamespace

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

pytestmark = pytest.mark.django_db

MB = 1024 * 1024


def _tag(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _refused(statement) -> bool:
    """Whether the database refuses `statement` as an append-only record. In a savepoint,
    so the test's own transaction stays usable."""
    try:
        with transaction.atomic():
            statement()
    except DatabaseError as exc:
        return "append-only" in str(exc)
    return False


# ===========================================================================
# 1. The ledger is append-only at the database
# ===========================================================================

def _ledger_row(make_user, amount="25.00", actor=None):
    from policy.money import MoneyType
    from wallet.models import Wallet, move_funds_atomic

    person = make_user(_tag("pd_led"))
    wallet = Wallet.objects.get(owner=person)
    row = move_funds_atomic(None, wallet, D(amount), tx_type="topup",
                            actor_id=getattr(actor, "pk", None),
                            money_type=MoneyType.WALLET_MOVE)
    return person, wallet, row


def test_the_ledger_refuses_update_and_delete_at_the_database(make_user):
    from wallet.models import Transaction

    _person, _wallet, row = _ledger_row(make_user)
    rows = Transaction.objects.filter(pk=row.pk)

    assert _refused(lambda: rows.update(amount=D("1.00")))
    assert _refused(lambda: rows.update(metadata={"rewritten": True}))
    assert _refused(lambda: rows.update(created_at=row.created_at - timedelta(days=9)))
    assert _refused(lambda: rows.delete())

    def by_hand():
        with connection.cursor() as cursor:
            cursor.execute("UPDATE wallet_transaction SET amount = 0 WHERE id = %s", [row.pk])
    assert _refused(by_hand)

    row.refresh_from_db()
    assert (row.amount, row.metadata) == (D("25.00"), {})


def test_a_ledger_row_is_never_saved_again_nor_deleted(make_user):
    from wallet.models import ImmutableRecord

    _person, _wallet, row = _ledger_row(make_user)
    row.metadata = {"rewritten": True}
    with pytest.raises(ImmutableRecord):
        row.save()
    with pytest.raises(ImmutableRecord):
        row.delete()


def test_only_the_actor_of_a_deleted_account_may_be_cleared(make_user):
    """`actor` is SET_NULL: deleting the account that acted rewrites it, and that is the
    one UPDATE the database allows — alone, never with anything else."""
    from wallet.models import Transaction, Wallet

    operator = make_user(_tag("pd_actor"))
    _person, _wallet, acted = _ledger_row(make_user, "5.00", actor=operator)
    # Nothing on the ledger names the operator's own wallet; its PROTECT would stop the
    # account's delete before the ledger was reached.
    Wallet.objects.filter(owner=operator).delete()
    operator.delete()

    acted.refresh_from_db()
    assert acted.actor_id is None and acted.amount == D("5.00")
    rows = Transaction.objects.filter(pk=acted.pk)
    assert _refused(lambda: rows.update(actor=None, amount=D("2.00")))
    assert _refused(lambda: rows.update(actor=make_user(_tag("pd_actor2"))))


def test_a_wallet_with_history_cannot_be_deleted(make_user):
    """`source_wallet` / `destination_wallet` are SET_NULL too, and may NOT change: a
    wallet's delete would have taken its history's link with it."""
    from wallet.models import Transaction, Wallet

    _person, wallet, row = _ledger_row(make_user)
    assert _refused(lambda: Wallet.objects.filter(pk=wallet.pk).delete())
    assert Wallet.objects.filter(pk=wallet.pk).exists()
    assert Transaction.objects.get(pk=row.pk).destination_wallet_id == wallet.pk


def test_an_exchange_names_its_pair_in_the_rows_it_writes(make_user):
    """The debit leg used to be written and then UPDATEd with its own reference."""
    from policy.money import MoneyType
    from wallet.models import (ExchangeRate, Wallet, exchange_funds_atomic,
                               move_funds_atomic, set_currency_for)

    ExchangeRate.objects.create(base="USD", quote="SYP", rate=D("13000.000000"),
                                effective_from=timezone.now() - timedelta(hours=1),
                                note="pd")
    payer, payee = make_user(_tag("pd_usd")), make_user(_tag("pd_syp"))
    set_currency_for(payer, "USD", "client")
    set_currency_for(payee, "SYP", "client")
    source, destination = Wallet.objects.get(owner=payer), Wallet.objects.get(owner=payee)
    move_funds_atomic(None, source, D("10.00"), tx_type="topup",
                      money_type=MoneyType.WALLET_MOVE)

    debit, credit = exchange_funds_atomic(source, destination, D("2.00"), amount_in="USD",
                                          money_type=MoneyType.WALLET_MOVE)
    debit.refresh_from_db()
    credit.refresh_from_db()
    assert debit.exchange_reference == debit.reference_id == credit.exchange_reference
    assert (debit.amount, credit.amount) == (D("2.00"), D("26000.00"))


def test_the_trigger_is_there_and_leaves_truncate_alone(make_user):
    """TRUNCATE fires no row trigger, so the suite's table resets and the thread tests'
    flushes still work."""
    _ledger_row(make_user)
    with connection.cursor() as cursor:
        cursor.execute("SELECT tgname FROM pg_trigger WHERE tgrelid = "
                       "'wallet_transaction'::regclass AND NOT tgisinternal")
        assert "wallet_transaction_append_only" in {name for (name,) in cursor.fetchall()}
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        cursor.execute("TRUNCATE wallet_transaction CASCADE")
        cursor.execute("SELECT count(*) FROM wallet_transaction")
        assert cursor.fetchone()[0] == 0


def test_a_test_backdates_a_row_only_through_the_owners_ddl(make_user):
    from tests.ledger_rows import backdate
    from wallet.models import Transaction

    _person, _wallet, row = _ledger_row(make_user)
    when = row.created_at - timedelta(days=4)
    backdate(row.pk, when)
    row.refresh_from_db()
    assert row.created_at == when
    # And the trigger is back on after it.
    assert _refused(lambda: Transaction.objects.filter(pk=row.pk).update(amount=D("3.00")))


# ===========================================================================
# 2. Erasure and export, bounded; files after the commit, off the request
# ===========================================================================

HEAVY = 3000          # rows of each heavy kind a long-time user holds
PAGE = 250            # PRIVACY_BATCH_ROWS for these tests
SETS_PER_DAY = 3      # set logs under each exercise-progress row


def _jpeg_bytes(w=64, h=64, shade=90):
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), (shade, 120, 150)).save(buf, "JPEG")
    return buf.getvalue()


@pytest.fixture
def heavy(make_user, settings, tmp_path):
    """A client with thousands of chat messages, set logs, activity rows and ledger rows,
    and a few chat photos in storage."""
    from analytics.models import UserActivity
    from chat.models import KIND_IMAGE, Conversation, Message
    from django.core.files.base import ContentFile
    from routine.models import Exercise, ExerciseSetLog, UserExerciseProgress
    from wallet.models import Transaction, Wallet

    settings.MEDIA_ROOT = str(tmp_path)
    settings.PRIVACY_BATCH_ROWS = PAGE
    client = make_user(_tag("pd_heavy"))
    trainer = make_user(_tag("pd_coach"), user_type="trainer")

    conversation = Conversation.objects.create(trainer=trainer, client=client)
    Message.objects.bulk_create(
        Message(conversation=conversation, sender=client if i % 2 else trainer,
                body=f"message {i} " + "x" * 200)
        for i in range(HEAVY))
    files = []
    for i in range(3):
        photo = Message(conversation=conversation, sender=client, kind=KIND_IMAGE)
        photo.attachment.save(f"photo{i}.jpg", ContentFile(_jpeg_bytes(shade=i)), save=False)
        photo.save()
        files.append(photo.attachment.name)

    exercise = Exercise.objects.create(name=_tag("pd heavy lift"), created_by=trainer)
    today = timezone.localdate()
    days = HEAVY // SETS_PER_DAY
    UserExerciseProgress.objects.bulk_create(
        UserExerciseProgress(user=client, exercise=exercise, date=today - timedelta(days=d),
                             target_sets=SETS_PER_DAY)
        for d in range(days))
    progress = list(UserExerciseProgress.objects.filter(user=client).order_by("pk"))
    ExerciseSetLog.objects.bulk_create(
        ExerciseSetLog(user_exercise_progress=row, set_number=n + 1, weight=40, reps=8,
                       date=row.date)
        for row in progress for n in range(SETS_PER_DAY))

    UserActivity.objects.bulk_create(
        UserActivity(user=client, activity_type="api_request", user_agent="pd/1.0 " * 20,
                     metadata={"path": f"/api/x/{i}"})
        for i in range(HEAVY))

    wallet = Wallet.objects.get(owner=client)
    Transaction.objects.bulk_create(
        Transaction(tx_type="topup", actor=client, destination_wallet=wallet,
                    amount=D("1.00"), currency=wallet.currency, money_type="WALLET_MOVE",
                    metadata={"n": i})
        for i in range(HEAVY))
    return SimpleNamespace(client=client, trainer=trainer, conversation=conversation,
                           files=files, wallet=wallet)


@contextmanager
def _largest_select():
    """The most rows any single SELECT returned inside the block."""
    seen = {"rows": 0, "sql": ""}

    def wrapper(execute, sql, params, many, context):
        result = execute(sql, params, many, context)
        cursor = context["cursor"]
        if sql.lstrip().upper().startswith("SELECT") and (cursor.rowcount or 0) > seen["rows"]:
            seen.update(rows=cursor.rowcount, sql=sql[:200])
        return result

    with connection.execute_wrapper(wrapper):
        yield seen


def test_erasing_a_heavy_user_never_loads_all_of_their_rows(
        heavy, api, django_capture_on_commit_callbacks):
    from analytics.models import UserActivity
    from chat.models import Message
    from routine.models import ExerciseSetLog
    from wallet.models import Transaction

    with _largest_select() as largest, django_capture_on_commit_callbacks(execute=True):
        response = api(heavy.client).delete("/api/privacy/erase/?confirm=ERASE")

    assert response.status_code == 200, response.content
    assert not Message.objects.filter(conversation=heavy.conversation).exists()
    assert not ExerciseSetLog.objects.filter(
        user_exercise_progress__user=heavy.client).exists()
    assert not UserActivity.objects.filter(user=heavy.client).exists()
    # The ledger is kept, whole.
    assert Transaction.objects.filter(actor=heavy.client).count() == HEAVY
    # One page of rows at a time, with what cascades from it (three sets a day): never
    # a whole table of a long-time user.
    assert largest["rows"] <= PAGE * SETS_PER_DAY, largest


def test_erasure_leaves_the_files_to_a_task_after_the_commit(
        heavy, api, settings, monkeypatch, django_capture_on_commit_callbacks):
    from django.core.files.storage import FileSystemStorage, default_storage

    from training_platform.privacy import tasks as privacy_tasks

    settings.STORAGE_DELETE_BATCH = 2
    queued = []
    task = getattr(privacy_tasks, "delete_stored_files", None)
    if task is not None:
        monkeypatch.setattr(task, "delay",
                            lambda label, names: queued.append((label, list(names))))
    deleted_in_request = []
    real_delete = FileSystemStorage.delete

    def watching(self, name):
        deleted_in_request.append(name)
        return real_delete(self, name)

    monkeypatch.setattr(FileSystemStorage, "delete", watching)
    with django_capture_on_commit_callbacks(execute=True):
        response = api(heavy.client).delete("/api/privacy/erase/?confirm=ERASE")
    assert response.status_code == 200, response.content

    assert not deleted_in_request, "files were deleted one by one inside the request"
    handed = [name for _label, chunk in queued for name in chunk]
    assert sorted(handed) == sorted(heavy.files), queued
    assert all(len(chunk) <= 2 for _label, chunk in queued)
    assert all(default_storage.exists(name) for name in heavy.files)

    # The task, as the worker runs it.
    for label, chunk in queued:
        task.apply(args=(label, chunk))
    assert not any(default_storage.exists(name) for name in heavy.files)


def test_the_file_task_retries_only_what_storage_refused(settings, tmp_path, monkeypatch):
    from django.core.files.base import ContentFile
    from django.core.files.storage import FileSystemStorage, default_storage

    from training_platform.privacy.tasks import delete_stored_files

    settings.MEDIA_ROOT = str(tmp_path)
    stuck, fine = (default_storage.save(f"chat/0/{key}.bin", ContentFile(b"x"))
                   for key in ("stuck", "fine"))
    refusals = {stuck: 2}
    asked = []
    real_delete = FileSystemStorage.delete

    def flaky(self, name):
        asked.append(name)
        if refusals.get(name):
            refusals[name] -= 1
            raise OSError("storage unavailable")
        return real_delete(self, name)

    monkeypatch.setattr(FileSystemStorage, "delete", flaky)
    # As a worker runs it: each retry is a new message carrying the retry's own args.
    from celery.exceptions import Retry

    args, retries, waits = ("chat.Message.attachment", [stuck, fine]), 0, []
    while True:
        assert retries < 10, "the task never finished"
        try:
            delete_stored_files.apply(args=args, retries=retries)
            break
        except Retry as retry:
            args, retries = retry.sig.args, retries + 1
            waits.append(retry.when)

    assert not default_storage.exists(stuck) and not default_storage.exists(fine)
    assert waits == sorted(waits) and waits[0] < waits[-1], "no backoff"
    assert asked.count(fine) == 1, "a deleted file was asked for again"
    assert asked.count(stuck) == 3


def test_a_rolled_back_erasure_hands_no_file_over(heavy, monkeypatch,
                                                   django_capture_on_commit_callbacks):
    from training_platform.privacy.registry import erase_user_data
    from training_platform.privacy import tasks as privacy_tasks

    queued = []
    monkeypatch.setattr(privacy_tasks.delete_stored_files, "delay",
                        lambda label, names: queued.append(names))
    with django_capture_on_commit_callbacks(execute=True):
        with pytest.raises(RuntimeError):
            with transaction.atomic():
                erase_user_data(heavy.client)
                raise RuntimeError("roll it back")
    assert not queued


def test_exporting_a_heavy_user_holds_a_page_at_a_time_and_says_everything(
        heavy, api, settings):
    settings.PRIVACY_EXPORT_SPOOL_BYTES = 256 * 1024
    caller = api(heavy.client)

    tracemalloc.start()
    try:
        response = caller.get("/api/privacy/export/")
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert response.status_code == 200
    assert "attachment" in response["Content-Disposition"]
    assert response["Cache-Control"] == "no-store"
    body = b"".join(response) if response.streaming else response.content
    data = json.loads(body)
    sections = data["sections"]
    assert data["user_id"] == heavy.client.pk
    assert len(sections["activity_log"]) == HEAVY
    assert len(sections["exercise_set_logs"]) == HEAVY
    assert len(sections["transactions"]) == HEAVY
    assert len(sections["messages_sent"]) == HEAVY // 2 + len(heavy.files)
    # In the section's own order, as before: newest first for the activity log.
    stamps = [row["timestamp"] for row in sections["activity_log"]]
    assert stamps == sorted(stamps, reverse=True)
    # Every row of four heavy tables as dicts is tens of megabytes; one page of them
    # and the section's ids are a small fraction of it.
    assert peak < 8 * MB, f"the export held {peak / MB:.1f} MB at its peak"


def test_a_small_export_is_one_body_as_before(make_user, api):
    person = make_user(_tag("pd_small"), specific_injury="left knee")
    response = api(person).get("/api/privacy/export/")
    assert response.status_code == 200 and not response.streaming
    assert response.json()["sections"]["profile"][0]["specific_injury"] == "left knee"


def test_the_many_to_many_ids_come_with_each_row(make_user):
    """Read a page at a time, one query per relation — not one per row."""
    from diet.models import FoodCategory, FoodItem, UserFoodPreference
    from training_platform.privacy.registry import export_user_data

    person = make_user(_tag("pd_m2m"))
    category = FoodCategory.objects.create(name=_tag("pd cat"))
    foods = [FoodItem.objects.create(api_id=_tag("pd-food"), name=_tag("pd food"),
                                     category=category, calories=100, protein=5,
                                     carbs=10, fat=2, serving_size="100 g",
                                     serving_size_grams=100)
             for _ in range(3)]
    preference = UserFoodPreference.objects.create(user=person)
    preference.liked_foods.set(foods[:2])
    row = export_user_data(person)["sections"]["food_preferences"][0]
    assert sorted(row["liked_foods"]) == sorted(f.pk for f in foods[:2])


# ===========================================================================
# 3. A per-process cap on concurrent image decodes
# ===========================================================================

def test_the_slots_are_what_the_memory_holds_at_the_decode_budget():
    from training_platform import image_budget as ib

    assert ib.slots_for(512 * MB) == 2
    assert ib.slots_for(1024 * MB) == 6
    assert ib.slots_for(2048 * MB, reserved=512 * MB) == 12
    assert ib.slots_for(256 * MB) == 1      # too small for one: refused at boot, not here
    assert ib.slots_for(None) == 1


def test_the_slot_count_is_stated_or_measured(settings):
    from training_platform import image_budget as ib

    settings.IMAGE_DECODE_SLOTS = None
    settings.IMAGE_DECODE_MEMORY_BYTES = 512 * MB
    settings.IMAGE_DECODE_RESERVED_BYTES = 192 * MB
    assert ib.configured_slots() == 2
    settings.IMAGE_DECODE_SLOTS = 3
    assert ib.configured_slots() == 3


@pytest.fixture
def decode_slots(settings):
    """Two decode slots in this process, a short wait for one, and a 7 s Retry-After."""
    from training_platform import image_budget

    settings.IMAGE_DECODE_WAIT_SECONDS = 0.2
    settings.IMAGE_DECODE_RETRY_AFTER_SECONDS = 7
    image_budget.reset_decode_slots(2)
    yield image_budget
    image_budget.reset_decode_slots()


class _Holders:
    """Threads that each take one decode slot and keep it until released."""

    def __init__(self, image_budget, count):
        self.release = threading.Event()
        self.ready = threading.Barrier(count + 1)
        self.threads = [threading.Thread(target=self._hold, args=(image_budget,), daemon=True)
                        for _ in range(count)]
        for thread in self.threads:
            thread.start()
        self.ready.wait(timeout=5)

    def _hold(self, image_budget):
        with image_budget.decode_slot(wait=5):
            self.ready.wait(timeout=5)
            self.release.wait(timeout=30)

    def done(self):
        self.release.set()
        for thread in self.threads:
            thread.join(timeout=5)


def _jpeg(name="photo.jpg", w=900, h=600, shade=90):
    return SimpleUploadedFile(name, _jpeg_bytes(w, h, shade), content_type="image/jpeg")


def test_a_full_house_answers_upload_busy_and_decodes_nothing(
        make_user, api, decode_slots, settings, tmp_path, monkeypatch):
    from training_platform import file_security

    settings.MEDIA_ROOT = str(tmp_path)
    decoded = []
    real = file_security._reencode

    def counting(*args, **kwargs):
        decoded.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(file_security, "_reencode", counting)
    person = make_user(_tag("pd_pic"))

    holders = _Holders(decode_slots, 2)
    try:
        busy = api(person).post("/api/auth/user/profile-picture/",
                                {"profile_picture": _jpeg("me.jpg")})
    finally:
        holders.done()

    assert busy.status_code == 503, busy.content
    body = busy.json()
    assert (body["code"], body["retry_after_seconds"]) == ("upload_busy", 7)
    assert busy["Retry-After"] == "7"
    assert body["detail"] and body["error"]
    assert not decoded, "the image was decoded while every slot was taken"
    person.refresh_from_db()
    assert not person.profile_picture

    # A slot free: the same upload goes through.
    fine = api(person).post("/api/auth/user/profile-picture/",
                            {"profile_picture": _jpeg("me.jpg")})
    assert fine.status_code == 200, fine.content
    assert decoded


def test_a_verification_photo_is_answered_busy_too(make_user, api, decode_slots, settings,
                                                   tmp_path):
    settings.MEDIA_ROOT = str(tmp_path / "media")
    settings.PRIVATE_MEDIA_ROOT = str(tmp_path / "private")
    trainer = make_user(_tag("pd_tv"), user_type="trainer")
    url = "/api/v1/trainer/verification/documents/photo/"

    holders = _Holders(decode_slots, 2)
    try:
        busy = api(trainer).post(url, {"file": _jpeg(shade=17)})
    finally:
        holders.done()
    assert (busy.status_code, busy.json()["code"]) == (503, "upload_busy"), busy.content
    assert busy["Retry-After"] == "7"

    fine = api(trainer).post(url, {"file": _jpeg(shade=18)})
    assert fine.status_code == 200, fine.content


def test_the_media_kit_takes_a_slot_for_its_own_decode_and_never_two_at_once(
        decode_slots, monkeypatch):
    """The guard's decode, then the kit's grade: two slots in turn, never nested — with
    ONE slot a nested take would wait and be refused."""
    from mediakit.processing import process_upload
    from mediakit.slots import resolve

    decode_slots.reset_decode_slots(1)
    entered = []
    real = decode_slots.decode_slot

    @contextmanager
    def counting(*args, **kwargs):
        with real(*args, **kwargs):
            entered.append(1)
            yield

    monkeypatch.setattr(decode_slots, "decode_slot", counting)
    processed = process_upload(_jpeg("avatar.jpg", 400, 400), resolve("trainer.avatar"))
    assert (processed.width, processed.height) == (400, 400)
    assert len(entered) == 2


def _production_boot(**overrides):
    from tests.test_media_storage import GCS_KEY_JSON, _production_boot as boot

    return boot(MEDIA_BACKEND="gcs", GS_BUCKET_NAME="yalla-gym-media",
                GS_CREDENTIALS_JSON=GCS_KEY_JSON, **overrides)


def test_production_refuses_more_decode_slots_than_the_memory_holds():
    booted = _production_boot(IMAGE_DECODE_MEMORY_BYTES=str(512 * MB))
    assert booted.returncode == 0, booted.stderr

    too_many = _production_boot(IMAGE_DECODE_MEMORY_BYTES=str(512 * MB),
                                IMAGE_DECODE_SLOTS="4")
    assert too_many.returncode != 0 and "IMAGE_DECODE_SLOTS" in too_many.stderr

    too_small = _production_boot(IMAGE_DECODE_MEMORY_BYTES=str(256 * MB))
    assert too_small.returncode != 0 and "one image decode" in too_small.stderr

    not_a_number = _production_boot(IMAGE_DECODE_SLOTS="two")
    assert not_a_number.returncode != 0 and "IMAGE_DECODE_SLOTS" in not_a_number.stderr


def test_production_refuses_a_periodic_page_the_soft_limit_would_cut_off():
    refused = _production_boot(PERIODIC_BATCH_SECONDS="200")
    assert refused.returncode != 0 and "PERIODIC_BATCH_SECONDS" in refused.stderr
    refused = _production_boot(PRIVACY_EXPORT_SPOOL_BYTES=str(512 * MB))
    assert refused.returncode != 0 and "PRIVACY_EXPORT_SPOOL_BYTES" in refused.stderr


# ===========================================================================
# 4. Periodic jobs over every user, a page per invocation
# ===========================================================================

def _drain(task, monkeypatch, measure, **first):
    """Run `task` invocation by invocation. Each hand-on is recorded instead of run, and
    run next; `measure()` is read after every invocation. Returns the readings."""
    queued = []
    monkeypatch.setattr(task, "apply_async",
                        lambda args=None, kwargs=None, **options: queued.append(kwargs or {}))
    readings = []
    task(**first)
    readings.append(measure())
    while queued:
        assert len(readings) < 50, "the walk never ended"
        task(**queued.pop(0))
        readings.append(measure())
    return readings


@pytest.fixture
def pages_of_two(settings):
    settings.PERIODIC_BATCH_SIZE = 2
    settings.PERIODIC_SCAN_SIZE = 2
    settings.PERIODIC_BATCH_SECONDS = 120


def test_daily_advice_is_written_a_page_at_a_time(make_user, monkeypatch, pages_of_two):
    from diet.models import DailyAdvice, DietPlan
    from diet.tasks import generate_daily_advice

    today = timezone.localdate()
    clients = [make_user(_tag("pd_adv"), preferred_language="en") for _ in range(5)]
    for client in clients:
        DietPlan.objects.create(user=client, goal="Maintain", daily_calories=2000,
                                start_date=today - timedelta(days=1),
                                end_date=today + timedelta(days=5))

    def written():
        return DailyAdvice.objects.filter(user__in=clients).count()

    assert _drain(generate_daily_advice, monkeypatch, written) == [2, 4, 5]
    # A redelivered page writes nothing twice.
    generate_daily_advice()
    assert written() == 5


def test_ai_insights_are_computed_a_page_of_users_at_a_time(make_user, monkeypatch,
                                                            pages_of_two):
    from ai_assistant.models import ChatSession, UserInsight
    from ai_assistant.tasks import compute_all_user_insights

    people = [make_user(_tag("pd_ins")) for _ in range(5)]
    for person in people:
        ChatSession.objects.create(user=person)

    def refreshed():
        return (UserInsight.objects.filter(user__in=people, insight_type="behavior_profile")
                .values("user").distinct().count())

    assert _drain(compute_all_user_insights, monkeypatch, refreshed) == [2, 4, 5]


def test_idle_ai_sessions_are_closed_a_page_at_a_time(make_user, monkeypatch, pages_of_two):
    from ai_assistant.models import ChatSession
    from ai_assistant.tasks import close_idle_sessions

    sessions = [ChatSession.objects.create(user=make_user(_tag("pd_idle")))
                for _ in range(5)]
    ChatSession.objects.filter(pk__in=[s.pk for s in sessions]).update(
        updated_at=timezone.now() - timedelta(hours=3))

    def closed():
        return ChatSession.objects.filter(pk__in=[s.pk for s in sessions],
                                          is_active=False).count()

    assert _drain(close_idle_sessions, monkeypatch, closed) == [2, 4, 5]


def test_the_weekly_progress_goes_out_a_page_of_people_at_a_time(make_user, api,
                                                                 monkeypatch, pages_of_two):
    from notifications.models import Notification
    from routine.outcomes import record_day
    from routine.tasks import send_weekly_progress
    from tests.test_strength_progress import _coached

    coached = [_coached(make_user, api, _tag("wk"), sets=2) for _ in range(3)]
    for _trainer, client, routine, _exercise in coached:
        record_day(client, routine, 1, timezone.localdate(), via="confirmed_unlogged",
                   confirmed_unlogged=True)
    people = [p for trainer, client, _r, _e in coached for p in (trainer, client)]

    def sent():
        return Notification.objects.filter(
            recipient__in=people,
            event_type__in=("weekly_recap", "trainer_weekly_digest")).count()

    readings = _drain(send_weekly_progress, monkeypatch, sent)
    assert readings[-1] == 6, readings
    assert len(readings) >= 3
    steps = [b - a for a, b in zip([0] + readings, readings)]
    assert max(steps) <= 2, steps
    # Once a week: a second run changes nothing.
    send_weekly_progress()
    assert sent() == 6


def test_workout_reminders_go_out_a_page_at_a_time(make_user, monkeypatch, pages_of_two):
    from notifications.models import Notification
    from notifications.tasks import send_workout_reminders
    from routine.models import Routine, RoutineProgress

    trainer = make_user(_tag("pd_rem_t"), user_type="trainer")
    routine = Routine.objects.create(name=_tag("pd reminders"), created_by=trainer, days=3,
                                     start_date=timezone.localdate() - timedelta(days=1))
    hour = timezone.now().astimezone(dt_timezone.utc).hour
    people = []
    for _ in range(5):
        person = make_user(_tag("pd_rem"), preferred_timezone="UTC",
                           workout_reminder_hour=hour)
        routine.assigned_to.add(person)
        RoutineProgress.objects.get_or_create(user=person, routine=routine, day=1,
                                              date=routine.start_date,
                                              defaults={"status": "not_started"})
        people.append(person)

    def reminded():
        return Notification.objects.filter(recipient__in=people,
                                           event_type="session_reminder").count()

    assert _drain(send_workout_reminders, monkeypatch, reminded) == [2, 4, 5]


def test_open_training_days_are_closed_a_page_at_a_time(make_user, monkeypatch,
                                                        pages_of_two):
    from routine.models import DayOutcome, RoutineProgress
    from routine.tasks import close_open_training_days
    from tests.test_day_outcomes import _program

    yesterday = timezone.localdate() - timedelta(days=1)
    clients = []
    for _ in range(5):
        _trainer, client, routine, _rows = _program(make_user, _tag("pd_open"), exercises=1)
        RoutineProgress.objects.create(user=client, routine=routine, day=1, date=yesterday,
                                       status="in_progress")
        clients.append(client)

    def recorded():
        return DayOutcome.objects.filter(user__in=clients, date=yesterday).count()

    assert _drain(close_open_training_days, monkeypatch, recorded) == [2, 4, 5]


def test_forgotten_sessions_are_all_abandoned_in_pages_not_one_page_a_night(
        make_user, monkeypatch, settings):
    from routine import tasks as routine_tasks
    from routine.models import Routine, WorkoutSession

    monkeypatch.setattr(routine_tasks, "SESSION_SWEEP_MAX", 2)
    trainer = make_user(_tag("pd_sess_t"), user_type="trainer")
    routine = Routine.objects.create(name=_tag("pd sessions"), created_by=trainer, days=1)
    long_ago = timezone.now() - timedelta(days=2)
    sessions = [WorkoutSession.objects.create(user=make_user(_tag("pd_sess")),
                                              routine=routine, start_time=long_ago)
                for _ in range(5)]

    assert routine_tasks.abandon_forgotten_sessions() == {"abandoned": 5, "capped": False}
    assert not WorkoutSession.objects.filter(pk__in=[s.pk for s in sessions],
                                             status="active").exists()


def test_subscription_reminders_go_out_a_page_at_a_time(make_user, monkeypatch,
                                                        pages_of_two, settings):
    from notifications.models import Notification
    from subscription.tasks import send_renewal_reminders
    from tests.test_subscription_freeze import _subscriber

    settings.SUBSCRIPTION_RENEWAL_NOTICE_DAYS = 3
    settings.SUBSCRIPTION_LAPSED_REMINDER_DAYS = (0, 3, 7, 14, 30)
    soon = [_subscriber(make_user, ends_in=timedelta(days=2, hours=12))[0] for _ in range(3)]
    lapsed = [_subscriber(make_user, status="expired", ends_in=-timedelta(hours=2))[0]
              for _ in range(2)]

    def told():
        return Notification.objects.filter(
            recipient__in=soon + lapsed,
            event_type__in=("subscription_expiring", "subscription_expired")).count()

    readings = _drain(send_renewal_reminders, monkeypatch, told)
    assert readings[-1] == 5, readings
    steps = [b - a for a, b in zip([0] + readings, readings)]
    assert max(steps) <= 2 and len(readings) >= 3, readings


def test_coaching_months_renew_a_page_of_relations_at_a_time(make_user, api, monkeypatch,
                                                             pages_of_two, settings):
    from tests.test_coaching_months import _accepted, _deliver_first_program, _end_month
    from users.models import CoachingPeriod
    from users.tasks import renew_coaching_periods

    settings.COACHING_PERIOD_DAYS = 30
    settings.COACHING_RENEWAL_REMINDER_DAYS = 3
    relations = []
    for _ in range(3):
        trainer, client, relation = _accepted(make_user, api, _tag("pdr"))
        _deliver_first_program(api, trainer, client)
        _end_month(relation)
        relations.append(relation)

    def renewed():
        return CoachingPeriod.objects.filter(relation__in=relations, number=2).count()

    assert _drain(renew_coaching_periods, monkeypatch, renewed) == [2, 3]
    # A redelivered page charges nothing twice.
    renew_coaching_periods()
    assert renewed() == 3
    assert CoachingPeriod.objects.filter(relation__in=relations, number=3).count() == 0


def test_ages_are_refreshed_a_page_of_rows_at_a_time(make_user, monkeypatch, pages_of_two):
    from datetime import date

    from users.models import CustomUser
    from users.tasks import refresh_ages_from_birth_dates

    people = [make_user(_tag("pd_age"), date_of_birth=date(1990, 1, 1)) for _ in range(5)]
    CustomUser.objects.filter(pk__in=[p.pk for p in people]).update(age=99)

    def refreshed():
        return CustomUser.objects.filter(pk__in=[p.pk for p in people]).exclude(age=99).count()

    readings = _drain(refresh_ages_from_birth_dates, monkeypatch, refreshed)
    assert readings[-1] == 5 and len(readings) >= 3, readings
    steps = [b - a for a, b in zip([0] + readings, readings)]
    assert max(steps) <= 2, steps


def test_expired_tokens_are_deleted_a_page_at_a_time(make_user, monkeypatch, settings):
    from rest_framework_simplejwt.token_blacklist.models import (BlacklistedToken,
                                                                 OutstandingToken)

    from training_platform.privacy.tasks import flush_expired_tokens

    settings.PERIODIC_SCAN_SIZE = 2
    settings.PERIODIC_BATCH_SECONDS = 0      # one page an invocation
    person = make_user(_tag("pd_tok"))
    past, future = timezone.now() - timedelta(days=1), timezone.now() + timedelta(days=1)
    expired = [OutstandingToken.objects.create(user=person, jti=uuid.uuid4().hex,
                                               token=f"t{i}", expires_at=past)
               for i in range(5)]
    for token in expired[:2]:
        BlacklistedToken.objects.create(token=token)
    alive = OutstandingToken.objects.create(user=person, jti=uuid.uuid4().hex,
                                            token="alive", expires_at=future)

    def left():
        return OutstandingToken.objects.filter(pk__in=[t.pk for t in expired]).count() \
            + BlacklistedToken.objects.filter(token__in=expired).count()

    readings = _drain(flush_expired_tokens, monkeypatch, left)
    assert readings[-1] == 0, readings
    removed = [a - b for a, b in zip([7] + readings, readings)]
    # A page of two rows per invocation (a blacklisted row goes with its token).
    assert max(removed) <= 2 * 2, removed
    assert len(readings) >= 3, readings
    assert OutstandingToken.objects.filter(pk=alive.pk).exists()


def test_food_weights_are_learned_a_page_of_users_at_a_time(make_user, monkeypatch,
                                                            pages_of_two):
    from diet.models import DietPlan
    from diet.planner import learning
    from diet.planner.tasks import refresh_food_weights

    today = timezone.localdate()
    people = [make_user(_tag("pd_fw")) for _ in range(5)]
    for person in people:
        DietPlan.objects.create(user=person, goal="Maintain", daily_calories=2000,
                                start_date=today, end_date=today + timedelta(days=6))
    seen = []
    real = learning.update_weights

    def watching(user):
        seen.append(user.pk)
        return real(user)

    monkeypatch.setattr(learning, "update_weights", watching)
    readings = _drain(refresh_food_weights, monkeypatch,
                      lambda: len([pk for pk in seen if pk in {p.pk for p in people}]))
    assert readings == [2, 4, 5]
    assert sorted(seen) == sorted(p.pk for p in people)

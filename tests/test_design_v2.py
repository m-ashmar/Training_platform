"""Design v2 backend: media slots, date of birth and the onboarding answers, trainer
proof, and trainer matching. See BACKEND_DELTA.md for what each item is and why.
"""
import io
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from PIL import Image

pytestmark = pytest.mark.django_db

JSON = "application/json"


@pytest.fixture(autouse=True)
def media_root(settings, tmp_path):
    """Uploads land in a temporary directory, not in the repository's media/."""
    settings.MEDIA_ROOT = str(tmp_path)
    return tmp_path


def _image(w, h, *, fmt="JPEG", transparent=False, orientation=None, name="shot"):
    if transparent:
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        img.paste((200, 60, 30, 255), (w // 4, h // 4, 3 * w // 4, 3 * h // 4))
    else:
        img = Image.new("RGB", (w, h), (200, 60, 30))
    buf = io.BytesIO()
    kwargs = {}
    if orientation:
        exif = Image.Exif()
        exif[0x0112] = orientation
        kwargs["exif"] = exif.tobytes()
    img.save(buf, format=fmt, **kwargs)
    ext = {"JPEG": "jpg", "PNG": "png"}[fmt]
    return SimpleUploadedFile(f"{name}.{ext}", buf.getvalue(),
                              content_type=f"image/{'jpeg' if fmt == 'JPEG' else 'png'}")


def _codes(node):
    """Every `code` inside a field_errors entry, whatever its nesting."""
    if isinstance(node, dict):
        found = [node["code"]] if "code" in node else []
        for value in node.values():
            found += _codes(value)
        return found
    if isinstance(node, list):
        return [c for item in node for c in _codes(item)]
    return []


def _years_ago(years, days=0):
    today = timezone.localdate()
    try:
        born = today.replace(year=today.year - years)
    except ValueError:  # Feb 29
        born = today.replace(year=today.year - years, day=28)
    return born - timedelta(days=days)


def _coach(make_user, username, **fields):
    base = dict(user_type="trainer", trainer_is_available=True,
                trainer_hourly_rate=Decimal("12000"))
    base.update(fields)
    return make_user(username, **base)


# ===================================================================== media slots
def test_a_portrait_is_graded_black_and_white_and_rendered_at_1x_and_2x(make_user):
    from mediakit.services import media_for, store

    trainer = make_user("dv2_media_t", user_type="trainer")
    asset = store(owner_type="trainer", owner_id=trainer.pk, slot_key="trainer.portrait",
                  upload=_image(1000, 1250), focal=(0.4, 0.3), alt_ar="رامي", alt_en="Rami")

    assert asset.person_id == trainer.pk
    one, two = Image.open(asset.file_1x), Image.open(asset.file_2x)
    assert (one.format, one.size) == ("WEBP", (400, 500))
    assert (two.format, two.size) == ("WEBP", (800, 1000))
    r, g, b = one.convert("RGB").getpixel((200, 250))
    assert max(r, g, b) - min(r, g, b) <= 2, "the grade must remove colour"
    assert asset.blur.startswith("data:image/webp;base64,") and len(asset.blur) < 1000
    assert 0 < asset.brightness < 1

    payload = media_for("trainer", trainer.pk)["trainer.portrait"]
    assert set(payload) == {"url_1x", "url_2x", "blur", "focal", "brightness",
                            "alt_ar", "alt_en"}
    assert payload["focal"] == [0.4, 0.3] and payload["alt_ar"] == "رامي"


@pytest.mark.parametrize("w,h,slot,code", [
    (1000, 1000, "trainer.portrait", "media_wrong_ratio"),
    (400, 500, "trainer.portrait", "media_too_small"),
    (960, 1280, "trainer.cutout", "media_needs_transparency"),
    (1000, 1250, "trainer.loop", "media_kind_not_supported_yet"),
    (1000, 1250, "trainer.signature", "media_kind_not_supported_yet"),
    (1000, 1250, "exercise.poster", "media_unknown_slot"),
    (1000, 1250, "app.story.9", "media_unknown_slot"),
])
def test_an_upload_that_does_not_fit_its_slot_is_refused_with_a_code(make_user, w, h, slot, code):
    from mediakit.models import MediaAsset
    from mediakit.services import store

    trainer = make_user("dv2_media_bad", user_type="trainer")
    owner = "app" if slot.startswith("app.") else "trainer"
    with pytest.raises(ValidationError) as refused:
        store(owner_type=owner, owner_id=trainer.pk, slot_key=slot, upload=_image(w, h))
    assert refused.value.code == code
    assert not MediaAsset.objects.exists()


def test_a_dish_keeps_its_colour(make_user):
    """Everything else is graded black and white; food is the owner's exception."""
    from diet.models import Recipe
    from mediakit.services import store

    dish = Recipe.objects.create(name="dv2 colour test dish", meal_types=["lunch"])
    asset = store(owner_type="dish", owner_id=dish.pk, slot_key="dish.top",
                  upload=_image(960, 1200))
    r, g, b = Image.open(asset.file_1x).convert("RGB").getpixel((240, 300))
    assert r - b > 100, "a dish must keep its colour"


def test_a_phone_photo_is_turned_upright_before_its_ratio_is_checked(make_user):
    """EXIF orientation 6: stored 1250 wide, shown 1000 wide. Read as stored, a 4:5
    portrait looks like 5:4 and is refused; the platform guard alone drops the tag."""
    from mediakit.services import store

    trainer = make_user("dv2_media_rot", user_type="trainer")
    asset = store(owner_type="trainer", owner_id=trainer.pk, slot_key="trainer.portrait",
                  upload=_image(1250, 1000, orientation=6))
    assert (asset.width, asset.height) == (1000, 1250)


def test_a_cutout_keeps_its_transparency(make_user):
    from mediakit.services import store

    trainer = make_user("dv2_media_cut", user_type="trainer")
    asset = store(owner_type="trainer", owner_id=trainer.pk, slot_key="trainer.cutout",
                  upload=_image(960, 1280, fmt="PNG", transparent=True))
    rendered = Image.open(asset.file_1x)
    assert rendered.mode == "RGBA"
    assert rendered.getpixel((2, 2))[3] == 0, "the corner must stay see-through"


def test_replacing_a_slot_removes_the_old_files_once_it_commits(
        make_user, django_capture_on_commit_callbacks):
    from mediakit.services import store

    trainer = make_user("dv2_media_rep", user_type="trainer")
    first = store(owner_type="trainer", owner_id=trainer.pk, slot_key="trainer.avatar",
                  upload=_image(500, 500))
    old = [first.original.name, first.file_1x.name, first.file_2x.name]
    with django_capture_on_commit_callbacks(execute=True):
        second = store(owner_type="trainer", owner_id=trainer.pk, slot_key="trainer.avatar",
                       upload=_image(600, 600))
    assert second.pk == first.pk
    assert not any(default_storage.exists(name) for name in old)
    assert default_storage.exists(second.file_1x.name)


def test_the_app_content_endpoint_is_public_and_lists_only_filled_slots(api):
    from mediakit.services import store

    store(owner_type="app", owner_id=0, slot_key="app.story.1", upload=_image(1080, 1920))
    resp = api().get("/api/v1/content/media/")
    assert resp.status_code == 200
    media = resp.json()["media"]
    assert list(media) == ["app.story.1"], "an empty slot is absent, never null"
    assert media["app.story.1"]["url_1x"].startswith("http")


def test_erasing_a_trainer_deletes_their_media_kit(make_user):
    from mediakit.models import MediaAsset
    from mediakit.services import store
    from training_platform.privacy import erase_user_data

    trainer = make_user("dv2_media_erase", user_type="trainer")
    store(owner_type="trainer", owner_id=trainer.pk, slot_key="trainer.avatar",
          upload=_image(500, 500))
    erase_user_data(trainer)
    assert not MediaAsset.objects.filter(owner_type="trainer", owner_id=trainer.pk).exists()


def test_the_console_uploads_through_the_same_checks(make_user):
    from django.test import Client

    from mediakit.models import MediaAsset

    operator = make_user("dv2_ops", user_type="admin")
    trainer = make_user("dv2_ops_t", user_type="trainer")
    console = Client()
    console.force_login(operator)
    form = {"owner_type": "trainer", "owner_id": trainer.pk, "slot": "trainer.portrait",
            "focal_x": 0.5, "focal_y": 0.35, "alt_ar": "", "alt_en": ""}

    refused = console.post("/dj-admin/mediakit/mediaasset/add/",
                           {**form, "upload": _image(1000, 1000)})
    assert refused.status_code == 200
    assert refused.context["adminform"].form.errors["upload"] == [
        "This slot needs a 4:5 image."]
    assert not MediaAsset.objects.exists()

    saved = console.post("/dj-admin/mediakit/mediaasset/add/",
                         {**form, "upload": _image(1000, 1250)})
    assert saved.status_code == 302
    asset = MediaAsset.objects.get()
    assert (asset.person_id, asset.uploaded_by_id, asset.focal_y) == (trainer.pk, operator.pk, 0.35)
    assert console.get("/dj-admin/mediakit/mediaasset/kits/").status_code == 200

    # Once saved, the picture cannot be moved to another slot or owner without a new
    # upload that passes that slot's checks; the focal point and alt text stay editable.
    other = make_user("dv2_ops_t2", user_type="trainer")
    changed = console.post(f"/dj-admin/mediakit/mediaasset/{asset.pk}/change/", {
        **form, "owner_id": other.pk, "slot": "trainer.wide", "focal_y": 0.2,
        "alt_en": "Rami, waist up"})
    assert changed.status_code == 302, changed.content
    asset.refresh_from_db()
    assert (asset.owner_id, asset.person_id, asset.slot) == (trainer.pk, trainer.pk,
                                                             "trainer.portrait")
    assert (asset.focal_y, asset.alt_en) == (0.2, "Rami, waist up")


# ======================================================= date of birth, onboarding
def _register(api, username, **extra):
    body = {"username": username, "email": f"{username}@gate.test",
            "phone_number": "+963999999999", "password1": "Xx!23456long",
            "password2": "Xx!23456long", "user_type": "client", **extra}
    return api().post("/api/auth/register/", body, content_type=JSON)


@pytest.mark.parametrize("born,code", [
    (lambda: _years_ago(14) + timedelta(days=1), "under_minimum_age"),
    (lambda: timezone.localdate() + timedelta(days=1), "date_of_birth_in_future"),
    (lambda: date(1900, 1, 1), "date_of_birth_implausible"),
    (None, "required"),   # the owner's decision of 2026-09-19
])
def test_sign_up_refuses_a_date_of_birth_it_must_not_accept(api, born, code):
    from users.models import CustomUser

    extra = {"date_of_birth": born().isoformat()} if born else {}
    resp = _register(api, "dv2_young", **extra)
    assert resp.status_code == 400
    assert _codes(resp.json()["field_errors"]["date_of_birth"]) == [code]
    assert not CustomUser.objects.filter(username="dv2_young").exists()


def test_sign_up_at_exactly_the_minimum_age_is_accepted_and_age_follows_the_date(api):
    from users.models import CustomUser

    born = _years_ago(14)
    assert _register(api, "dv2_fourteen", date_of_birth=born.isoformat()).status_code == 201
    user = CustomUser.objects.get(username="dv2_fourteen")
    assert (user.date_of_birth, user.age) == (born, 14)


def test_the_nightly_refresh_moves_age_on_a_birthday(make_user):
    from users.models import CustomUser
    from users.tasks import refresh_ages_from_birth_dates

    user = make_user("dv2_bday", date_of_birth=_years_ago(30))
    assert user.age == 30
    CustomUser.objects.filter(pk=user.pk).update(age=29)  # a birthday nobody saved on
    assert refresh_ages_from_birth_dates() >= 1
    user.refresh_from_db()
    assert user.age == 30
    assert refresh_ages_from_birth_dates() == 0


@pytest.mark.parametrize("path", ["/api/auth/user/update/", "/api/auth/client/profile/"])
@pytest.mark.parametrize("age", [13, 12, 0])
def test_every_written_age_below_the_minimum_says_so(make_user, api, path, age):
    resp = api(make_user("dv2_under")).post(path, {"age": age}, content_type=JSON)
    assert resp.status_code == 400
    assert _codes(resp.json()["field_errors"]["age"]) == ["under_minimum_age"]


def test_a_date_of_birth_owns_age_and_cannot_be_removed(make_user, api):
    from users.models import age_on

    user = make_user("dv2_age")
    client = api(user)

    ok = client.post("/api/auth/user/update/", {"date_of_birth": "2000-01-01", "age": 99},
                     content_type=JSON)
    assert ok.status_code == 200, ok.content
    user.refresh_from_db()
    assert user.age == age_on(date(2000, 1, 1))

    client.post("/api/auth/user/update/", {"age": 50}, content_type=JSON)
    user.refresh_from_db()
    assert user.age == age_on(date(2000, 1, 1)), "a date of birth decides age"

    cleared = client.post("/api/auth/user/update/", {"date_of_birth": None}, content_type=JSON)
    assert _codes(cleared.json()["field_errors"]["date_of_birth"]) == ["date_of_birth_required"]


def test_the_onboarding_answers_are_kept_and_the_trainer_sees_them_but_not_the_date(
        make_user, api):
    from django.db import connection

    from users.models import TrainerClientRelation

    client = make_user("dv2_onb_c", date_of_birth=date(2000, 1, 1))
    resp = api(client).post("/api/auth/client/profile/", {
        "training_goal": "build_muscle", "training_place": "gym",
        "training_days_per_week": 4, "injury_areas": ["knee", "shoulder"]}, content_type=JSON)
    assert resp.status_code == 200, resp.content
    client.refresh_from_db()
    assert client.injury_areas == ["knee", "shoulder"]
    assert client.resolve_fitness_goal() == "Gain"
    with connection.cursor() as cur:
        cur.execute("SELECT injury_areas FROM users_customuser WHERE id = %s", [client.pk])
        assert "knee" not in cur.fetchone()[0], "injury areas are encrypted at rest"

    trainer = make_user("dv2_onb_t", user_type="trainer")
    TrainerClientRelation.objects.create(trainer=trainer, client=client, status="approved",
                                         initiated_by="client")
    client.assigned_trainer = trainer
    client.save()
    seen = api(trainer).get(f"/api/auth/trainer/client-profile/{client.pk}/")
    assert seen.status_code == 200, seen.content
    body = seen.json()
    assert (body["training_goal"], body["training_place"], body["training_days_per_week"],
            body["injury_areas"]) == ("build_muscle", "gym", 4, ["knee", "shoulder"])
    assert "date_of_birth" not in body and body["age"] == client.age


@pytest.mark.parametrize("areas,expected", [(["none"], []), ([], [])])
def test_no_injury_is_an_empty_list(make_user, api, areas, expected):
    user = make_user("dv2_none", injury_areas=["knee"])
    resp = api(user).post("/api/auth/client/profile/", {"injury_areas": areas}, content_type=JSON)
    assert resp.status_code == 200, resp.content
    user.refresh_from_db()
    assert user.injury_areas == expected


@pytest.mark.parametrize("areas,code", [
    (["knee", "knee"], "duplicate"),
    (["none", "knee"], "none_with_areas"),
    (["elbow"], "invalid_choice"),
])
def test_a_bad_injury_list_is_refused_with_a_code(make_user, api, areas, code):
    user = make_user("dv2_badareas")
    resp = api(user).post("/api/auth/client/profile/", {"injury_areas": areas}, content_type=JSON)
    assert resp.status_code == 400
    assert _codes(resp.json()["field_errors"]["injury_areas"]) == [code]


def test_onboarding_missing_names_what_is_left_and_completed_keeps_its_meaning(make_user, api):
    user = make_user("dv2_missing", first_name="Sam", last_name="Haddad", height=178,
                     weight=82, age=27, gender="Male")
    body = api(user).get("/api/auth/user/update/").json()
    assert body["onboarding_completed"] is True
    assert body["onboarding_missing"] == ["date_of_birth", "training_goal", "training_place",
                                          "training_days_per_week"]


def test_a_trainer_sets_matching_codes_and_neither_side_writes_the_others_fields(make_user, api):
    trainer = make_user("dv2_codes_t", user_type="trainer")
    ok = api(trainer).post("/api/auth/trainer/profile/", {
        "trainer_goal_focus": ["strength", "nutrition"], "trainer_injury_focus": ["knee"],
        "trainer_training_places": ["gym", "home"]}, content_type=JSON)
    assert ok.status_code == 200, ok.content
    trainer.refresh_from_db()
    assert trainer.trainer_goal_focus == ["strength", "nutrition"]
    bad = api(trainer).post("/api/auth/trainer/profile/", {"trainer_goal_focus": ["yoga"]},
                            content_type=JSON)
    assert _codes(bad.json()["field_errors"]["trainer_goal_focus"]) == ["invalid_choice"]

    api(trainer).post("/api/auth/user/update/", {"training_goal": "strength"}, content_type=JSON)
    client = make_user("dv2_codes_c")
    api(client).post("/api/auth/user/update/", {"trainer_goal_focus": ["strength"],
                                                "training_goal": "strength"}, content_type=JSON)
    trainer.refresh_from_db()
    client.refresh_from_db()
    assert trainer.training_goal == "" and client.trainer_goal_focus == []
    assert client.training_goal == "strength"


def test_export_carries_the_new_answers_and_erasure_clears_them(make_user):
    from training_platform.privacy import erase_user_data, export_user_data

    user = make_user("dv2_priv", date_of_birth=date(1999, 3, 14), injury_areas=["knee"],
                     training_goal="fat_loss", training_days_per_week=3)
    profile = export_user_data(user)["sections"]["profile"][0]
    assert (profile["date_of_birth"], profile["injury_areas"], profile["training_goal"]) == \
        (date(1999, 3, 14), ["knee"], "fat_loss")
    erase_user_data(user)
    user.refresh_from_db()
    assert (user.date_of_birth, user.injury_areas, user.training_goal,
            user.training_days_per_week) == (None, [], "", None)


# ======================================================================== proof
def _answered(trainer, client, minutes, *, initiated_by="client", days_ago=1):
    from users.models import TrainerRequestOutcome

    answered = timezone.now() - timedelta(days=days_ago)
    TrainerRequestOutcome.objects.create(
        trainer=trainer, client=client, initiated_by=initiated_by, outcome="approved",
        requested_at=answered - timedelta(minutes=minutes), responded_at=answered)


def test_a_request_is_stamped_and_its_answer_outlives_the_relation(make_user, api):
    from users.models import TrainerClientRelation, TrainerRequestOutcome

    trainer = _coach(make_user, "dv2_ans_t", trainer_hourly_rate=None)
    client = make_user("dv2_ans_c")
    asked = api(client).post("/api/auth/client/request-trainer/", {"trainer_id": trainer.pk},
                             content_type=JSON)
    assert asked.status_code == 200, asked.content
    relation = TrainerClientRelation.objects.get(trainer=trainer, client=client)
    assert relation.requested_at is not None and relation.responded_at is None

    TrainerClientRelation.objects.filter(pk=relation.pk).update(
        requested_at=timezone.now() - timedelta(minutes=90))
    answered = api(trainer).post("/api/auth/trainer/respond-to-request/",
                                 {"request_id": relation.pk, "action": "approve"},
                                 content_type=JSON)
    assert answered.status_code == 200, answered.content
    relation.refresh_from_db()
    assert relation.status == "approved" and relation.responded_at is not None
    outcome = TrainerRequestOutcome.objects.get(trainer=trainer)
    assert outcome.outcome == "approved" and 89 * 60 <= outcome.response_seconds <= 91 * 60

    relation.delete()
    assert TrainerRequestOutcome.objects.filter(trainer=trainer).exists()


def test_typical_response_is_the_median_of_client_requests_and_needs_three(make_user):
    from users.trainer_proof import response_times

    trainer = _coach(make_user, "dv2_rt_t")
    clients = [make_user(f"dv2_rt_c{i}") for i in range(5)]
    _answered(trainer, clients[0], 30)
    _answered(trainer, clients[1], 100)
    assert response_times([trainer.pk])[trainer.pk] == (None, 2)
    _answered(trainer, clients[2], 200)
    _answered(trainer, clients[3], 5, initiated_by="trainer")  # the client answered this one
    _answered(trainer, clients[4], 10, days_ago=120)           # outside the window
    assert response_times([trainer.pk])[trainer.pk] == (100, 3)


def test_a_client_counts_as_finished_only_after_every_day_of_the_routine(make_user):
    from routine.models import Routine, WorkoutSession
    from users.trainer_proof import completed_programs

    trainer, rival = _coach(make_user, "dv2_fin_t"), _coach(make_user, "dv2_fin_r")
    client = make_user("dv2_fin_c")
    routine = Routine.objects.create(name="Push Pull Legs", created_by=trainer, days=3)
    elsewhere = Routine.objects.create(name="Other", created_by=rival, days=1)
    for day in (1, 2, 2):
        WorkoutSession.objects.create(user=client, routine=routine, day=day, status="completed")
    WorkoutSession.objects.create(user=client, routine=routine, day=3, status="abandoned")
    WorkoutSession.objects.create(user=client, routine=elsewhere, day=1, status="active")
    assert completed_programs([trainer.pk, rival.pk]) == {trainer.pk: 0, rival.pk: 0}

    WorkoutSession.objects.create(user=client, routine=routine, day=3, status="completed")
    assert completed_programs([trainer.pk, rival.pk]) == {trainer.pk: 1, rival.pk: 0}


# ==================================================================== matching
def _match(api, client, **query):
    params = "&".join(f"{k}={v}" for k, v in {"limit": 10, **query}.items())
    return api(client).get(f"/api/v1/trainers/matches?{params}")


def _mine(results, *trainers):
    ids = {t.pk for t in trainers}
    return [r for r in results if r["trainer"]["id"] in ids]


def test_matches_rank_the_trainer_who_fits_and_say_why_in_both_languages(make_user, api):
    client = make_user("dv2_m_c", training_goal="build_muscle", training_place="gym",
                       training_days_per_week=4, injury_areas=["shoulder"])
    fit = _coach(make_user, "dv2_m_fit", trainer_goal_focus=["build_muscle", "strength"],
                 trainer_injury_focus=["shoulder"], trainer_training_places=["gym"])
    other = _coach(make_user, "dv2_m_other", trainer_goal_focus=["fat_loss"],
                   trainer_experience_years=11)

    resp = _match(api, client)
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["criteria"] == {"training_goal": "build_muscle", "training_place": "gym",
                                "training_days_per_week": 4}
    first, second = _mine(body["results"], fit, other)
    assert first["trainer"]["id"] == fit.pk and first["rank"] < second["rank"]
    assert [r["code"] for r in first["reasons"]] == ["goal_match", "injury_experience",
                                                     "training_place"]
    assert first["reasons"][0]["text"] == {"ar": "متخصص في هدفك: بناء العضل",
                                           "en": "Specialises in your goal: building muscle"}
    assert first["reasons"][1]["text"]["ar"] == "خبرة مع إصابات الكتف"
    assert [r["code"] for r in second["reasons"]] == ["experience"]
    assert second["reasons"][0]["text"] == {"ar": "11 سنة خبرة",
                                            "en": "11 years of experience"}
    assert "email" not in first["trainer"]


def test_program_days_and_response_time_are_reasons_too(make_user, api):
    from routine.models import Routine

    client = make_user("dv2_days_c", training_days_per_week=4)
    trainer = _coach(make_user, "dv2_days_t")
    Routine.objects.create(name="Four days", created_by=trainer, days=4)
    for i, minutes in enumerate((30, 60, 90)):
        _answered(trainer, make_user(f"dv2_days_asker{i}"), minutes)

    (row,) = _mine(_match(api, client).json()["results"], trainer)
    assert [r["code"] for r in row["reasons"]] == ["program_days", "fast_response"]
    assert row["reasons"][0]["text"]["ar"] == "برامج 4 أيام"
    assert row["reasons"][1]["text"] == {"ar": "يرد خلال ساعة عادةً",
                                         "en": "Usually replies within 1 hour"}
    assert row["trainer"]["proof"]["typical_response_minutes"] == 60


def test_the_goal_is_read_from_free_text_until_the_trainer_picks_codes(make_user, api):
    client = make_user("dv2_infer_c", training_goal="build_muscle")
    trainer = _coach(make_user, "dv2_infer_t", trainer_specializations=["كمال أجسام"])
    (row,) = _mine(_match(api, client).json()["results"], trainer)
    assert row["trainer"]["focus"] == ["build_muscle"]
    assert row["reasons"][0]["code"] == "goal_match"


def test_a_trainer_not_taking_clients_or_without_a_verified_address_is_not_matched(make_user, api):
    client = make_user("dv2_excl_c")
    closed = _coach(make_user, "dv2_excl_closed", trainer_is_available=False)
    unproven = _coach(make_user, "dv2_excl_unproven", email_verified=False)
    assert _mine(_match(api, client).json()["results"], closed, unproven) == []


def test_first_place_needs_a_complete_media_kit_when_anyone_has_one(make_user, api):
    from mediakit.models import MediaAsset
    from mediakit.slots import required_keys

    client = make_user("dv2_kit_c", training_goal="strength")
    best = _coach(make_user, "dv2_kit_best", trainer_goal_focus=["strength"])
    kitted = _coach(make_user, "dv2_kit_kitted")
    for slot in required_keys("trainer"):
        MediaAsset.objects.create(owner_type="trainer", owner_id=kitted.pk, person=kitted,
                                  slot=slot, original="x.jpg", file_1x="x.webp",
                                  file_2x="x.webp")
    results = _match(api, client).json()["results"]
    assert results[0]["trainer"]["id"] == kitted.pk
    assert set(results[0]["trainer"]["media"]) == set(required_keys("trainer"))
    assert _mine(results, best)[0]["rank"] == 2


def test_only_a_signed_in_client_is_matched(make_user, api):
    trainer = _coach(make_user, "dv2_only_t")
    refused = _match(api, trainer)
    assert refused.status_code == 403 and refused.json()["code"] == "clients_only"
    assert api().get("/api/v1/trainers/matches").status_code == 401
    client = make_user("dv2_only_c")
    for limit in ("0", "11", "abc", "\u00b2", "1" * 5000):
        bad = api(client).get("/api/v1/trainers/matches", {"limit": limit})
        assert bad.status_code == 400 and bad.json()["code"] == "invalid_limit", limit


def test_a_trainer_who_stores_odd_json_cannot_break_the_roster_or_matching(make_user, api):
    """`trainer_specializations` and `trainer_certifications` are unvalidated JSON."""
    client = make_user("dv2_odd_c", training_goal="strength")
    number = _coach(make_user, "dv2_odd_num", first_name="Oddly",
                    trainer_specializations=1, trainer_certifications=True)
    text = _coach(make_user, "dv2_odd_str", first_name="Oddly",
                  trainer_specializations="Strength coach", trainer_certifications="CPT")

    roster = api().get("/api/v1/trainers/?search=Oddly")
    assert roster.status_code == 200, roster.content
    cards = {c["id"]: c for c in roster.json()["results"]}
    assert cards[number.pk]["specializations"] == []
    assert cards[text.pk]["specializations"] == ["Strength coach"]
    assert cards[text.pk]["focus"] == ["strength"]
    assert api().get(f"/api/v1/trainers/{number.pk}/").json()["certifications"] == []
    assert api().get(f"/api/v1/trainers/{text.pk}/").json()["certifications"] == ["CPT"]
    assert _match(api, client).status_code == 200


@pytest.mark.parametrize("channel,shown", [("apk", True), ("play", True), ("ios", False),
                                           ("", False)])
def test_a_coaching_fee_reaches_only_a_build_that_may_show_it(make_user, api, channel, shown):
    trainer = _coach(make_user, "dv2_price_t", first_name="Pricey")
    client = make_user("dv2_price_c")
    cards = [
        next(c for c in api().get("/api/v1/trainers/?search=Pricey",
                                  HTTP_X_APP_CHANNEL=channel).json()["results"]
             if c["id"] == trainer.pk),
        api().get(f"/api/v1/trainers/{trainer.pk}/", HTTP_X_APP_CHANNEL=channel).json(),
        _mine(api(client).get("/api/v1/trainers/matches?limit=10",
                              HTTP_X_APP_CHANNEL=channel).json()["results"],
              trainer)[0]["trainer"],
    ]
    for card in cards:
        assert ("coaching_price" in card) is shown and ("money_type" in card) is shown
        if shown:
            assert (card["coaching_price"], card["money_type"]) == ("12000.00", "COACHING_1TO1")


def test_the_roster_is_the_same_for_every_viewer_on_one_build(make_user, api):
    _coach(make_user, "dv2_same_t", first_name="Samey")
    viewers = [make_user("dv2_same_a"), make_user("dv2_same_b", user_type="trainer"), None]
    bodies = [api(v).get("/api/v1/trainers/?search=Samey").json() for v in viewers]
    assert bodies[0] == bodies[1] == bodies[2] and bodies[0]["count"] == 1


def test_the_roster_shows_trainers_not_taking_clients_and_filters(make_user, api):
    open_ = _coach(make_user, "dv2_ros_open", first_name="Rosty",
                   trainer_goal_focus=["strength"], trainer_is_verified=True)
    closed = _coach(make_user, "dv2_ros_closed", first_name="Rosty",
                    trainer_is_available=False, trainer_specializations=["Weight loss"])
    _coach(make_user, "dv2_ros_hidden", first_name="Rosty", email_verified=False)

    body = api().get("/api/v1/trainers/?search=Rosty").json()
    assert {"count", "next", "previous", "results"} <= set(body)
    assert [c["id"] for c in body["results"]] == [open_.pk, closed.pk]
    assert body["results"][1]["proof"]["accepting_clients"] is False
    assert all("email" not in c for c in body["results"])

    def ids(query):
        return [c["id"] for c in api().get(f"/api/v1/trainers/?search=Rosty&{query}")
                .json()["results"]]
    assert ids("focus=strength") == [open_.pk]
    assert ids("focus=fat_loss") == [closed.pk]
    assert ids("verified=true") == [open_.pk]
    bad = api().get("/api/v1/trainers/?focus=yoga")
    assert bad.status_code == 400 and bad.json()["code"] == "invalid_focus"


def test_a_profile_shows_bio_certifications_and_the_library(make_user, api):
    from routine.models import Exercise

    trainer = _coach(make_user, "dv2_prof_t", trainer_bio="Eight years of strength coaching.",
                     trainer_certifications=["CPT"])
    for name in ("Barbell bench press", "Back squat", "Deadlift", "Pull-up"):
        Exercise.objects.create(name=name, created_by=trainer)
    Exercise.objects.create(name="Retired move", created_by=trainer, is_active=False)
    body = api().get(f"/api/v1/trainers/{trainer.pk}/").json()
    assert body["bio"] == "Eight years of strength coaching."
    assert body["certifications"] == ["CPT"]
    assert body["library"]["exercise_count"] == 4 and len(body["library"]["preview"]) == 3
    assert "Retired move" not in [e["name"] for e in body["library"]["preview"]]

    client = make_user("dv2_prof_c")
    unproven = _coach(make_user, "dv2_prof_unproven", email_verified=False)
    for pk in (client.pk, unproven.pk):
        missing = api().get(f"/api/v1/trainers/{pk}/")
        assert missing.status_code == 404 and missing.json()["code"] == "not_found"


def test_the_roster_and_matches_cost_the_same_queries_for_3_or_12_trainers(make_user, api):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    client = make_user("dv2_q_c", training_goal="strength")

    def cost():
        counts = []
        for call in (lambda: api().get("/api/v1/trainers/?search=Crowd"),
                     lambda: api(client).get("/api/v1/trainers/matches?limit=10")):
            with CaptureQueriesContext(connection) as ctx:
                assert call().status_code == 200
            counts.append(len(ctx))
        return counts

    for i in range(3):
        _coach(make_user, f"dv2_q{i}", first_name="Crowd")
    small = cost()
    for i in range(3, 12):
        _coach(make_user, f"dv2_q{i}", first_name="Crowd")
    # The ORM cache (cachalot) answers a read or two differently between runs; a query
    # per trainer would add nine.
    large = cost()
    assert all(big <= little + 2 for big, little in zip(large, small)), (small, large)

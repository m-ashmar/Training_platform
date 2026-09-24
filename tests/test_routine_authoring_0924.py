"""The trainer's program builder, from the server (BACKEND_BUGS B-102..B-105, B-107,
B-108, B-111; design `13-trainer-programs`).

Each test fails on 20418df for the reason its docstring gives — a key the row never
carried, a route that did not exist, a value the columns could not hold — and passes
after. Every refusal asserted here names its own `code` (the owner's decision 9,
2026-09-24).
"""
import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

pytestmark = pytest.mark.django_db

JSON = "application/json"


# ------------------------------------------------------------------------ helpers
def _trainer_and_client(make_user, tag, *, approved=True):
    from users.models import TrainerClientRelation

    trainer = make_user(f"ra_t_{tag}", user_type="trainer")
    client = make_user(f"ra_c_{tag}")
    if approved:
        TrainerClientRelation.objects.create(trainer=trainer, client=client,
                                             status="approved")
    return trainer, client


def _exercise(owner, name, **kw):
    from routine.models import Exercise

    return Exercise.objects.create(name=name, created_by=owner, **kw)


def _routine(trainer, tag, *, days=14, client=None):
    from routine.models import Routine

    routine = Routine.objects.create(name=f"ra {tag}", created_by=trainer, days=days,
                                     start_date=timezone.localdate())
    if client is not None:
        routine.assigned_to.add(client)
    return routine


def _row(routine, exercise, day, order=1, **kw):
    from routine.models import RoutineExercise

    kw.setdefault("sets", 3)
    kw.setdefault("reps", 10)
    return RoutineExercise.objects.create(routine=routine, exercise=exercise, day=day,
                                          order=order, **kw)


def _results(resp):
    body = resp.json()
    return body["results"] if isinstance(body, dict) and "results" in body else body


def _field_code(resp, field):
    body = resp.json()
    return body["field_errors"][field][0]["code"]


def _warm_request_memos(caller, path):
    """Setup for the query-count tests, not an assertion. Every signed-in request
    memoises two per-user reads for a minute (training_platform/middleware.py: the rate
    limiter's tier, and the response cache's "is this account still usable"), so the
    FIRST request a user makes costs two queries the next one does not — whatever the
    page holds. Warming them on ANOTHER cached route, one sharing no SQL with the page
    measured (so cachalot serves the page nothing), leaves the comparison measuring
    rows alone."""
    assert caller.get(path).status_code == 200


# ============================================================ B-102 whose exercise
def test_b102_an_exercise_row_says_whose_it_is(make_user, api):
    """`GET exercises/` merged the trainer's own exercises with the shared catalogue and
    nothing on the row said which was which: no `is_mine`, no `created_by_id`."""
    trainer, client = _trainer_and_client(make_user, "b102")
    other = make_user("ra_t_b102_other", user_type="trainer")
    mine = _exercise(trainer, "aaa ra b102 mine")
    catalogue = _exercise(None, "aaa ra b102 catalogue")

    resp = api(trainer).get("/api/routine/exercises/?page_size=100")
    assert resp.status_code == 200, resp.content
    rows = _results(resp)
    assert rows and all({"is_mine", "created_by_id"} <= set(r) for r in rows)
    assert all(r["is_mine"] == (r["created_by_id"] == trainer.pk) for r in rows)

    own = api(trainer).get(f"/api/routine/exercises/{mine.pk}/").json()
    assert (own["is_mine"], own["created_by_id"]) == (True, trainer.pk)
    shared = api(trainer).get(f"/api/routine/exercises/{catalogue.pk}/").json()
    assert (shared["is_mine"], shared["created_by_id"]) == (False, None)

    # `is_mine` is for the READER: the same exercise, read by the trainer's client
    # inside the routine the trainer wrote, is not the client's.
    routine = _routine(trainer, "b102", days=1, client=client)
    _row(routine, mine, 1)
    resp = api(client).get(f"/api/routine/routine-exercises/?routine={routine.pk}")
    assert resp.status_code == 200, resp.content
    nested = _results(resp)[0]["exercise"]
    assert nested["is_mine"] is False
    assert nested["created_by_id"] == trainer.pk

    # And another trainer reading the catalogue never owns the trainer's row.
    resp = api(other).get(f"/api/routine/exercises/{catalogue.pk}/")
    assert resp.json()["is_mine"] is False


def test_b102_whose_exercise_costs_no_query_per_row(make_user, api):
    """Cheap: a page of six of your own exercises costs what a page of two costs."""
    trainer = make_user("ra_t_b102q", user_type="trainer")
    for n in range(2):
        _exercise(trainer, f"aaa ra b102q {n}")
    _warm_request_memos(api(trainer), "/api/routine/templates/")

    def page_queries():
        client = api(trainer)
        with CaptureQueriesContext(connection) as ctx:
            resp = client.get("/api/routine/exercises/?page_size=100")
        assert resp.status_code == 200
        assert all("is_mine" in r for r in _results(resp))
        return len(ctx.captured_queries)

    two = page_queries()
    for n in range(2, 6):
        _exercise(trainer, f"aaa ra b102q {n}")
    assert page_queries() == two


# ======================================================= B-107 names and equipment
def test_b107_an_exercise_carries_both_names_and_its_equipment(make_user, api):
    """One name on the wire and no equipment: TrainerNewExercise's Arabic and English
    names could not both be written, and the tile's «الصدر · بار» had no «بار»."""
    trainer = make_user("ra_t_b107", user_type="trainer")
    resp = api(trainer).post("/api/routine/exercises/", {
        "name_ar": "ضغط الأكتاف بالدمبل", "name_en": "Dumbbell shoulder press",
        "target_muscle": "Front Deltoid", "equipment": "dumbbell",
    }, content_type=JSON)
    assert resp.status_code == 201, resp.content
    body = resp.json()
    assert body["name_ar"] == "ضغط الأكتاف بالدمبل"
    assert body["name_en"] == "Dumbbell shoulder press"
    assert body["equipment"] == "dumbbell"
    assert body["equipment_display"] == "Dumbbell"
    assert body["is_mine"] is True

    # `name` keeps its meaning: the reader's language, the other one as a fallback.
    arabic = api(trainer).get(f"/api/routine/exercises/{body['id']}/",
                              HTTP_ACCEPT_LANGUAGE="ar").json()
    assert arabic["name"] == "ضغط الأكتاف بالدمبل"
    assert arabic["equipment_display"] == "دمبل"
    english = api(trainer).get(f"/api/routine/exercises/{body['id']}/",
                               HTTP_ACCEPT_LANGUAGE="en").json()
    assert english["name"] == "Dumbbell shoulder press"

    # Writing `name` alone still writes the reader's language, as it did.
    resp = api(trainer).patch(f"/api/routine/exercises/{body['id']}/",
                              {"name": "ضغط بالدمبل"}, content_type=JSON,
                              HTTP_ACCEPT_LANGUAGE="ar")
    assert resp.status_code == 200, resp.content
    assert resp.json()["name_ar"] == "ضغط بالدمبل"
    assert resp.json()["name_en"] == "Dumbbell shoulder press"

    # Equipment is a code from the design's list, or null.
    bad = api(trainer).patch(f"/api/routine/exercises/{body['id']}/",
                             {"equipment": "kettlebell"}, content_type=JSON)
    assert bad.status_code == 400
    assert _field_code(bad, "equipment") == "invalid_choice"
    cleared = api(trainer).patch(f"/api/routine/exercises/{body['id']}/",
                                 {"equipment": None}, content_type=JSON)
    assert cleared.status_code == 200 and cleared.json()["equipment"] is None

    # No name in any language is the refusal it always was.
    nameless = api(trainer).post("/api/routine/exercises/", {"target_muscle": "Biceps"},
                                 content_type=JSON)
    assert nameless.status_code == 400
    assert _field_code(nameless, "name") == "required"


def test_b107_the_upload_form_takes_both_names_and_equipment(make_user, api):
    """`exercises/create-with-image/` is the form TrainerNewExercise posts; it read
    `name` only and had nowhere to put the equipment."""
    trainer = make_user("ra_t_b107u", user_type="trainer")
    resp = api(trainer).post("/api/routine/exercises/create-with-image/", {
        "name": "Cable fly", "name_ar": "تفتيح بالكابل", "name_en": "Cable fly",
        "description": "d", "target_muscle": "Middle Chest", "equipment": "cable",
    })
    assert resp.status_code == 201, resp.content
    exercise = resp.json()["exercise"]
    assert exercise["name_ar"] == "تفتيح بالكابل"
    assert exercise["name_en"] == "Cable fly"
    assert exercise["equipment"] == "cable"
    assert exercise["is_mine"] is True
    assert exercise["created_by_id"] == trainer.pk

    bad = api(trainer).post("/api/routine/exercises/create-with-image/", {
        "name": "x", "description": "d", "target_muscle": "Biceps", "equipment": "rope",
    })
    assert bad.status_code == 400
    assert bad.json()["code"] == "equipment_invalid"


# ============================================================ B-111 author's id
def test_b111_a_routine_names_its_author_by_id(make_user, api):
    """`created_by` was only a display string, so the app could not tell which programs
    a frozen coaching holds (`training_refusal` keys on the author's id)."""
    trainer, client = _trainer_and_client(make_user, "b111")
    routine = _routine(trainer, "b111", days=3, client=client)

    as_client = api(client).get(f"/api/routine/routines/{routine.pk}/").json()
    assert as_client["created_by_id"] == trainer.pk
    assert isinstance(as_client["created_by"], str)          # unchanged, still display text

    as_trainer = api(trainer).get(f"/api/routine/routines/{routine.pk}/").json()
    assert as_trainer["created_by_id"] == trainer.pk
    listed = _results(api(client).get("/api/routine/routines/"))
    assert {r["created_by_id"] for r in listed if r["id"] == routine.pk} == {trainer.pk}


# ======================================================== B-108 template usage
def test_b108_a_template_says_how_many_clients_used_it(make_user, api):
    """TrainerRoutines prints «6 عملاء استخدموه» on every template card; the row had no
    such count."""
    from routine.models import RoutineTemplate

    trainer, first = _trainer_and_client(make_user, "b108a")
    second = make_user("ra_c_b108b")
    third = make_user("ra_c_b108c")
    stranger = make_user("ra_t_b108_stranger", user_type="trainer")
    template = RoutineTemplate.objects.create(name="ra b108", goal="strength", days=3,
                                              created_by=trainer, is_public=True)
    for n, client in enumerate([first, second, first]):    # `first` twice: counted once
        routine = _routine(trainer, f"b108 {n}", days=3, client=client)
        routine.source_template = template
        routine.save(update_fields=["source_template"])
    unassigned = _routine(trainer, "b108 draft", days=3)     # a copy nobody is on
    unassigned.source_template = template
    unassigned.save(update_fields=["source_template"])
    # A client on a routine NOT written from the template does not count.
    _routine(trainer, "b108 other", days=3, client=third)

    mine = api(trainer).get(f"/api/routine/templates/{template.pk}/").json()
    assert mine["used_by_count"] == 2
    listed = _results(api(trainer).get("/api/routine/templates/my_templates/"))
    assert [t["used_by_count"] for t in listed if t["id"] == template.pk] == [2]

    # Another trainer's business: they read the public template, not its numbers.
    theirs = api(stranger).get(f"/api/routine/templates/{template.pk}/").json()
    assert theirs["used_by_count"] is None


def test_b108_the_count_costs_no_query_per_template(make_user, api):
    from routine.models import RoutineTemplate

    trainer, client = _trainer_and_client(make_user, "b108q")

    def add(n):
        template = RoutineTemplate.objects.create(name=f"ra b108q {n}", goal="x", days=1,
                                                  created_by=trainer)
        routine = _routine(trainer, f"b108q {n}", days=1, client=client)
        routine.source_template = template
        routine.save(update_fields=["source_template"])

    def page_queries():
        caller = api(trainer)
        with CaptureQueriesContext(connection) as ctx:
            resp = caller.get("/api/routine/templates/my_templates/")
        assert resp.status_code == 200
        assert all(t["used_by_count"] == 1 for t in _results(resp))
        return len(ctx.captured_queries)

    add(0)
    _warm_request_memos(api(trainer), "/api/routine/exercises/")
    one = page_queries()
    add(1)
    add(2)
    assert page_queries() == one


# ============================================= B-104 supersets, load modes, days
def test_b104_a_row_holds_its_superset_and_load_mode(make_user, api):
    """A row had sets, reps, rest, weight, day, order and notes: the superset bracket and
    the «% من الأقصى» / RPE modes could not round-trip."""
    trainer, client = _trainer_and_client(make_user, "b104")
    routine = _routine(trainer, "b104", days=7, client=client)
    bench = _exercise(trainer, "ra b104 bench")
    dips = _exercise(trainer, "ra b104 dips")
    caller = api(trainer)

    percent = caller.post("/api/routine/routine-exercises/", {
        "routine": routine.pk, "exercise_id": bench.pk, "day": 1, "order": 1,
        "sets": 4, "reps": 8, "weight": 66, "load_mode": "percent", "load_value": 80,
        "superset_group": 1,
    }, content_type=JSON)
    assert percent.status_code == 201, percent.content
    body = percent.json()
    assert (body["load_mode"], body["load_value"], body["weight"]) == ("percent", 80, 66)
    assert body["superset_group"] == 1

    rpe = caller.post("/api/routine/routine-exercises/", {
        "routine": routine.pk, "exercise_id": dips.pk, "day": 1, "order": 2,
        "load_mode": "rpe", "load_value": 8.5, "superset_group": 1,
    }, content_type=JSON)
    assert rpe.status_code == 201, rpe.content
    assert (rpe.json()["load_mode"], rpe.json()["load_value"]) == ("rpe", 8.5)
    assert rpe.json()["weight"] is None

    # A plain row is kg, with no load value: `weight` is the whole prescription.
    plain = _row(routine, dips, 2)
    got = caller.get(f"/api/routine/routine-exercises/{plain.pk}/").json()
    assert (got["load_mode"], got["load_value"], got["superset_group"]) == ("kg", None, None)

    # The client reads the same row.
    rows = _results(api(client).get(f"/api/routine/routine-exercises/?routine={routine.pk}&day=1"))
    assert {(r["load_mode"], r["superset_group"]) for r in rows} == {("percent", 1), ("rpe", 1)}

    def refused(payload, field, code, row=percent.json()["id"]):
        resp = caller.patch(f"/api/routine/routine-exercises/{row}/", payload,
                            content_type=JSON)
        assert resp.status_code == 400, (payload, resp.content)
        assert _field_code(resp, field) == code, resp.content

    refused({"load_value": 0}, "load_value", "load_value_out_of_range")
    refused({"load_value": 101}, "load_value", "load_value_out_of_range")
    refused({"load_mode": "rpe", "load_value": 11}, "load_value", "load_value_out_of_range")
    refused({"load_mode": "rpe", "load_value": 7.3}, "load_value", "load_value_out_of_range")
    refused({"load_value": None}, "load_value", "load_value_required")
    refused({"load_mode": "percent", "load_value": None}, "load_value", "load_value_required")
    refused({"load_mode": "kg", "load_value": 70}, "load_value", "load_value_not_allowed")
    refused({"superset_group": 0}, "superset_group", "superset_group_out_of_range")
    refused({"superset_group": 21}, "superset_group", "superset_group_out_of_range")
    refused({"load_mode": "pounds"}, "load_mode", "invalid_choice")
    fresh = caller.post("/api/routine/routine-exercises/", {
        "routine": routine.pk, "exercise_id": bench.pk, "day": 3, "order": 1,
        "load_mode": "percent"}, content_type=JSON)
    assert fresh.status_code == 400 and _field_code(fresh, "load_value") == "load_value_required"

    # Back to kg: the value that belonged to the percentage goes with it.
    back = caller.patch(f"/api/routine/routine-exercises/{percent.json()['id']}/",
                        {"load_mode": "kg"}, content_type=JSON)
    assert back.status_code == 200, back.content
    assert (back.json()["load_mode"], back.json()["load_value"], back.json()["weight"]) \
        == ("kg", None, 66)
    # Out of the superset.
    alone = caller.patch(f"/api/routine/routine-exercises/{rpe.json()['id']}/",
                         {"superset_group": None}, content_type=JSON)
    assert alone.status_code == 200 and alone.json()["superset_group"] is None


def test_b104_the_database_holds_the_mode_and_its_value_together(make_user):
    """A percentage without its number, or kg with one, is not a prescription: the row
    itself refuses it, whatever path writes it."""
    from django.core.exceptions import ValidationError
    from django.db import IntegrityError, transaction

    from routine.models import RoutineExercise

    trainer = make_user("ra_t_b104db", user_type="trainer")
    routine = _routine(trainer, "b104db", days=1)
    exercise = _exercise(trainer, "ra b104db")
    with pytest.raises(ValidationError):
        _row(routine, exercise, 1, load_mode="percent")
    with pytest.raises(IntegrityError), transaction.atomic():
        RoutineExercise.objects.bulk_create([RoutineExercise(
            routine=routine, exercise=exercise, day=1, order=1, load_mode="kg",
            load_value=50)])


def test_b104_a_routine_names_its_days(make_user, api):
    """«دفع · صدر وأكتاف»: a routine had no per-day name. Writes MERGE by day, so two
    phones naming two different days cannot erase each other's name."""
    trainer, client = _trainer_and_client(make_user, "b104d")
    routine = _routine(trainer, "b104d", days=7, client=client)
    caller = api(trainer)

    first = caller.patch(f"/api/routine/routines/{routine.pk}/",
                         {"day_names": {"1": "دفع", "3": "سحب"}}, content_type=JSON)
    assert first.status_code == 200, first.content
    assert first.json()["day_names"] == {"1": "دفع", "3": "سحب"}

    second = caller.patch(f"/api/routine/routines/{routine.pk}/",
                          {"day_names": {"3": None, "4": "  أرجل  "}}, content_type=JSON)
    assert second.status_code == 200, second.content
    assert second.json()["day_names"] == {"1": "دفع", "4": "أرجل"}

    # The client's Train tab reads them too.
    assert api(client).get(f"/api/routine/routines/{routine.pk}/").json()["day_names"] \
        == {"1": "دفع", "4": "أرجل"}
    assert caller.get(f"/api/routine/routines/{routine.pk}/").json()["day_names"] \
        == {"1": "دفع", "4": "أرجل"}

    def refused(names, code):
        resp = caller.patch(f"/api/routine/routines/{routine.pk}/", {"day_names": names},
                            content_type=JSON)
        assert resp.status_code == 400, (names, resp.content)
        assert _field_code(resp, "day_names") == code, resp.content

    refused({"8": "x"}, "day_out_of_range")
    refused({"0": "x"}, "day_out_of_range")
    refused({"one": "x"}, "day_names_invalid")
    refused(["دفع"], "day_names_invalid")
    refused({"2": 5}, "day_names_invalid")
    refused({"2": "x" * 41}, "too_long")

    # Shortening the routine drops the names of the days it no longer has.
    shorter = caller.patch(f"/api/routine/routines/{routine.pk}/", {"days": 3},
                           content_type=JSON)
    assert shorter.status_code == 200, shorter.content
    assert shorter.json()["day_names"] == {"1": "دفع"}


def test_b104_copying_a_routine_keeps_supersets_load_modes_and_day_names(make_user, api):
    """`routines/{id}/copy/` («ابدأ من برنامجه الحالي») must not flatten the program."""
    from routine.models import Routine

    trainer, client = _trainer_and_client(make_user, "b104c")
    routine = _routine(trainer, "b104c", days=7, client=client)
    Routine.objects.filter(pk=routine.pk).update(day_names={"1": "دفع"})
    exercise = _exercise(trainer, "ra b104c")
    _row(routine, exercise, 1, weight=66, load_mode="percent", load_value=80,
         superset_group=2)

    resp = api(trainer).post(f"/api/routine/routines/{routine.pk}/copy/")
    assert resp.status_code == 201, resp.content
    copy = resp.json()
    assert copy["day_names"] == {"1": "دفع"}
    row = copy["routine_exercises"][0]
    assert (row["load_mode"], row["load_value"], row["weight"], row["superset_group"]) \
        == ("percent", 80, 66, 2)


# =============================================================== B-103 copy week
def _two_week_program(make_user, tag):
    """A 14-day routine (two calendar weeks) with week 1 written: day 1 a percentage
    in superset 1 with a note, day 3 in kg, day 5 by RPE with no weight."""
    from routine.models import Routine

    trainer, client = _trainer_and_client(make_user, tag)
    routine = _routine(trainer, tag, days=14, client=client)
    Routine.objects.filter(pk=routine.pk).update(day_names={"1": "دفع", "3": "سحب",
                                                            "10": "باقٍ"})
    bench = _exercise(trainer, f"ra {tag} bench")
    row_ = _exercise(trainer, f"ra {tag} row")
    dips = _exercise(trainer, f"ra {tag} dips")
    _row(routine, bench, 1, order=1, sets=4, reps=8, weight=66, load_mode="percent",
         load_value=80, superset_group=1, notes="ببطء", rest_time=120)
    _row(routine, dips, 1, order=2, sets=3, reps=12, superset_group=1)
    _row(routine, row_, 3, order=1, weight=18)
    _row(routine, dips, 5, order=1, load_mode="rpe", load_value=8)
    return trainer, client, routine


def test_b103_copy_week_writes_the_next_week_with_heavier_loads(make_user, api):
    """There was no week copy: the app wrote one `routine-exercises/` POST per row (24
    writes for a 6 x 4 week) and rolled back by hand on the first refusal."""
    from routine.models import RoutineExercise

    trainer, client, routine = _two_week_program(make_user, "b103")
    resp = api(trainer).post(f"/api/routine/routines/{routine.pk}/copy_week/",
                             {"from_week": 1, "to_week": 2, "load_factor": 1.025},
                             content_type=JSON)
    assert resp.status_code == 201, resp.content
    body = resp.json()
    assert body["copied"] == 4
    assert (body["from_week"], body["to_week"], body["load_factor"]) == (1, 2, 1.025)

    week2 = {(r.day, r.order): r for r in RoutineExercise.objects.filter(
        routine=routine, day__gte=8)}
    assert set(week2) == {(8, 1), (8, 2), (10, 1), (12, 1)}
    bench = week2[(8, 1)]
    # 66 kg x 1.025 = 67.65, to the nearest 0.5 kg; 80 % x 1.025 = 82 %.
    assert (bench.weight, bench.load_mode, bench.load_value) == (67.5, "percent", 82.0)
    assert (bench.sets, bench.reps, bench.rest_time, bench.notes, bench.superset_group) \
        == (4, 8, 120, "ببطء", 1)
    assert week2[(8, 2)].weight is None and week2[(8, 2)].superset_group == 1
    assert week2[(10, 1)].weight == 18.5                      # 18.45 -> 18.5
    # An RPE is an effort, not a load: it is copied as written.
    assert (week2[(12, 1)].load_mode, week2[(12, 1)].load_value) == ("rpe", 8)
    # Week 1 is untouched.
    assert RoutineExercise.objects.get(routine=routine, day=1, order=1).weight == 66

    # The rows come back ready to merge into the grid.
    assert sorted(r["day"] for r in body["routine_exercises"]) == [8, 8, 10, 12]
    assert all(r["id"] for r in body["routine_exercises"])
    # Day names travel with their days, and never overwrite a name already there.
    assert body["day_names"] == {"1": "دفع", "3": "سحب", "8": "دفع", "10": "باقٍ"}

    # The client reads the new week.
    rows = _results(api(client).get(f"/api/routine/routine-exercises/?routine={routine.pk}&day=8"))
    assert len(rows) == 2


def test_b103_same_loads_are_copied_exactly(make_user, api):
    """«نفس الأوزان» (factor 1, the default) copies a load as written, unrounded."""
    from routine.models import RoutineExercise

    trainer, _client, routine = _two_week_program(make_user, "b103same")
    RoutineExercise.objects.filter(routine=routine, day=3).update(weight=18.3)
    resp = api(trainer).post(f"/api/routine/routines/{routine.pk}/copy_week/",
                             {"from_week": 1, "to_week": 2}, content_type=JSON)
    assert resp.status_code == 201, resp.content
    assert resp.json()["load_factor"] == 1.0
    assert RoutineExercise.objects.get(routine=routine, day=10).weight == 18.3
    assert RoutineExercise.objects.get(routine=routine, day=8, order=1).load_value == 80


def test_b103_a_filled_week_is_never_overwritten(make_user, api):
    """The target week holds rows: refused whole, nothing written, nothing replaced."""
    from routine.models import RoutineExercise

    trainer, _client, routine = _two_week_program(make_user, "b103full")
    _row(routine, _exercise(trainer, "ra b103full extra"), 9)
    before = list(RoutineExercise.objects.filter(routine=routine)
                  .order_by("id").values_list("id", "day", "weight"))
    resp = api(trainer).post(f"/api/routine/routines/{routine.pk}/copy_week/",
                             {"from_week": 1, "to_week": 2, "load_factor": 1.05},
                             content_type=JSON)
    assert resp.status_code == 409, resp.content
    assert resp.json()["code"] == "week_not_empty"
    assert resp.json()["to_week"] == 2
    assert list(RoutineExercise.objects.filter(routine=routine)
                .order_by("id").values_list("id", "day", "weight")) == before


def test_b103_every_refusal_names_its_code(make_user, api):
    from routine.models import RoutineExercise

    trainer, client, routine = _two_week_program(make_user, "b103codes")
    other_trainer = make_user("ra_t_b103codes_other", user_type="trainer")
    admin = make_user("ra_admin_b103codes", user_type="admin")
    url = f"/api/routine/routines/{routine.pk}/copy_week/"
    count = RoutineExercise.objects.filter(routine=routine).count()

    def refused(caller, payload, status, code):
        resp = caller.post(url, payload, content_type=JSON)
        assert resp.status_code == status, (payload, resp.content)
        assert resp.json()["code"] == code, (payload, resp.content)
        return resp.json()

    mine = api(trainer)
    assert refused(mine, {"to_week": 2}, 400, "week_invalid")["field"] == "from_week"
    assert refused(mine, {"from_week": 1, "to_week": "two"}, 400,
                   "week_invalid")["field"] == "to_week"
    refused(mine, {"from_week": 0, "to_week": 2}, 400, "week_invalid")
    refused(mine, {"from_week": 1.5, "to_week": 2}, 400, "week_invalid")
    out = refused(mine, {"from_week": 1, "to_week": 3}, 400, "week_out_of_range")
    assert (out["field"], out["weeks"]) == ("to_week", 2)
    refused(mine, {"from_week": 2, "to_week": 2}, 400, "same_week")
    refused(mine, {"from_week": 2, "to_week": 1}, 400, "week_empty")
    refused(mine, {"from_week": 1, "to_week": 2, "load_factor": "more"}, 400,
            "load_factor_invalid")
    refused(mine, {"from_week": 1, "to_week": 2, "load_factor": 1.6}, 400,
            "load_factor_out_of_range")
    refused(mine, {"from_week": 1, "to_week": 2, "load_factor": 0.4}, 400,
            "load_factor_out_of_range")
    # A load the copy would push past what a row may hold is refused, whole.
    RoutineExercise.objects.filter(routine=routine, day=3).update(weight=990)
    refused(mine, {"from_week": 1, "to_week": 2, "load_factor": 1.05}, 400,
            "load_out_of_range")
    RoutineExercise.objects.filter(routine=routine, day=3).update(weight=18)

    # Only the trainer who wrote it: the client it is for, an admin — refused by name.
    refused(api(client), {"from_week": 1, "to_week": 2}, 403, "not_routine_owner")
    refused(api(admin), {"from_week": 1, "to_week": 2}, 403, "not_routine_owner")
    # A trainer who cannot see it at all learns nothing about it.
    resp = api(other_trainer).post(url, {"from_week": 1, "to_week": 2}, content_type=JSON)
    assert resp.status_code == 404
    assert api().post(url, {"from_week": 1, "to_week": 2},
                      content_type=JSON).status_code == 401

    assert RoutineExercise.objects.filter(routine=routine).count() == count


def test_b103_a_routine_shorter_than_a_week_has_no_weeks(make_user, api):
    """A hand-made 3-day cycle is not a calendar program: it has no whole week to copy
    into, and says so instead of writing days the routine does not have."""
    trainer, _client = _trainer_and_client(make_user, "b103short")
    routine = _routine(trainer, "b103short", days=10)
    _row(routine, _exercise(trainer, "ra b103short"), 1)
    resp = api(trainer).post(f"/api/routine/routines/{routine.pk}/copy_week/",
                             {"from_week": 1, "to_week": 2}, content_type=JSON)
    assert resp.status_code == 400
    assert (resp.json()["code"], resp.json()["weeks"]) == ("week_out_of_range", 1)


# ============================================================ B-105 injury map
def test_b105_the_injury_map_is_served(make_user, api):
    """No server map from an injury area to the muscles it argues with: the amber chip
    was decided by a table on the phone, broader than the artboard."""
    from routine.models import Exercise
    from users.models import INJURY_AREAS

    trainer = make_user("ra_t_b105", user_type="trainer")
    resp = api(trainer).get("/api/routine/injury-map/")
    assert resp.status_code == 200, resp.content
    body = resp.json()
    areas = {a["code"]: a["muscles"] for a in body["areas"]}
    assert set(areas) == {code for code, _label in INJURY_AREAS}

    muscles = {key for key, _label in Exercise._meta.get_field("target_muscle").choices}
    assert all(set(m) <= muscles for m in areas.values())
    # The artboard: a shoulder injury flags the overhead press, not the bench press,
    # the lateral raise or the incline fly beside it.
    assert "Front Deltoid" in areas["shoulder"]
    assert not {"Middle Chest", "Lateral Deltoid", "Upper Chest"} & set(areas["shoulder"])
    # A condition the client describes in words is not mapped to muscles.
    assert areas["other"] == []
    # No lighter-load number the server cannot justify.
    assert "lighter_load" not in body and all("lighter_load" not in a for a in body["areas"])
    assert isinstance(body["version"], int)

    # A client may read it too; nobody signed out may.
    assert api(make_user("ra_c_b105")).get("/api/routine/injury-map/").status_code == 200
    assert api().get("/api/routine/injury-map/").status_code == 401


# ============================================ added while finishing the rows (0924)
def test_b103_a_trainer_may_copy_a_week_for_a_frozen_client(make_user, api, settings):
    """The freeze stops the CLIENT training, never the trainer writing
    (users/coaching.py; tests/test_coaching_freeze.py
    `test_a_trainer_may_still_prepare_work_for_a_frozen_client`): a week's copy is a
    routine write like any other, so the trainer prepares the next weeks while unpaid."""
    from routine.models import Routine, RoutineExercise
    from tests.test_coaching_freeze import _coached, _freeze
    from tests.test_coaching_months import OWNER_RATE
    from users.coaching import training_refusal

    settings.COACHING_COMMISSION_RATE = OWNER_RATE
    settings.COACHING_PERIOD_DAYS = 30
    settings.COACHING_RENEWAL_REMINDER_DAYS = 3
    settings.COACHING_FROZEN_REMINDER_DAYS = (3, 7, 14, 30)
    trainer, client, relation, routine, *_ = _coached(make_user, api, "ra_frz")
    Routine.objects.filter(pk=routine.pk).update(days=14)
    _freeze(relation)
    routine.refresh_from_db()
    assert training_refusal(client, routine) == "coaching_frozen"

    resp = api(trainer).post(f"/api/routine/routines/{routine.pk}/copy_week/",
                             {"from_week": 1, "to_week": 2}, content_type=JSON)
    assert resp.status_code == 201, resp.content
    assert RoutineExercise.objects.filter(routine=routine, day=8).count() == 1
    # The client still reads it and still may not copy it.
    refused = api(client).post(f"/api/routine/routines/{routine.pk}/copy_week/",
                               {"from_week": 1, "to_week": 2}, content_type=JSON)
    assert (refused.status_code, refused.json()["code"]) == (403, "not_routine_owner")


def test_b103_a_trainer_who_sees_the_routine_but_did_not_write_it_is_refused(make_user, api):
    """The one predicate decides what a trainer SEES (an approved relation with the
    client on it); writing a week is the author's alone. The client left the author for
    a second coach, who now sees the old program and may not rewrite it."""
    from users.models import TrainerClientRelation

    first, client, routine = _two_week_program(make_user, "b103seen")
    TrainerClientRelation.objects.filter(trainer=first, client=client).update(
        status="rejected")
    second = make_user("ra_t_b103seen_second", user_type="trainer")
    TrainerClientRelation.objects.create(trainer=second, client=client, status="approved")
    assert api(second).get(f"/api/routine/routines/{routine.pk}/").status_code == 200
    resp = api(second).post(f"/api/routine/routines/{routine.pk}/copy_week/",
                            {"from_week": 1, "to_week": 2}, content_type=JSON)
    assert (resp.status_code, resp.json()["code"]) == (403, "not_routine_owner")


def test_b103_a_percentage_the_copy_would_push_past_100_is_refused_whole(make_user, api):
    from routine.models import RoutineExercise

    trainer, _client, routine = _two_week_program(make_user, "b103pct")
    RoutineExercise.objects.filter(routine=routine, day=1, order=1).update(load_value=98)
    before = RoutineExercise.objects.filter(routine=routine).count()
    resp = api(trainer).post(f"/api/routine/routines/{routine.pk}/copy_week/",
                             {"from_week": 1, "to_week": 2, "load_factor": "1.05"},
                             content_type=JSON)
    assert resp.status_code == 400, resp.content
    assert (resp.json()["code"], resp.json()["day"]) == ("load_out_of_range", 8)
    assert RoutineExercise.objects.filter(routine=routine).count() == before


def test_b104_a_new_routine_takes_its_day_names(make_user, api):
    trainer = make_user("ra_t_b104new", user_type="trainer")
    resp = api(trainer).post("/api/routine/routines/", {
        "name": "ra b104new", "days": 4, "day_names": {"1": " دفع ", "2": None, "4": "أرجل"},
    }, content_type=JSON)
    assert resp.status_code == 201, resp.content
    assert resp.json()["day_names"] == {"1": "دفع", "4": "أرجل"}
    assert resp.json()["created_by_id"] == trainer.pk
    bad = api(trainer).post("/api/routine/routines/", {
        "name": "ra b104new bad", "days": 2, "day_names": {"3": "x"}}, content_type=JSON)
    assert bad.status_code == 400 and _field_code(bad, "day_names") == "day_out_of_range"


def test_b107_names_sent_alone_still_fill_the_untranslated_column(make_user, api):
    """`.values('exercise__name')` reads the raw column (the analyzers group by it): an
    exercise written only as `name_ar` / `name_en` must not leave it empty, and an
    Arabic reader's English name must not land in `name_ar`."""
    from routine.models import Exercise

    trainer = make_user("ra_t_b107raw", user_type="trainer")
    resp = api(trainer).post("/api/routine/exercises/", {
        "name_en": "Hack squat", "target_muscle": "Front Quads"}, content_type=JSON,
        HTTP_ACCEPT_LANGUAGE="ar")
    assert resp.status_code == 201, resp.content
    raw = Exercise.objects.filter(pk=resp.json()["id"]).values_list(
        "name", "name_ar", "name_en").get()
    assert raw == ("Hack squat", None, "Hack squat")

    upload = api(trainer).post("/api/routine/exercises/create-with-image/", {
        "name_ar": "سكوات أمامي", "description": "d", "target_muscle": "Front Quads",
    }, HTTP_ACCEPT_LANGUAGE="en")
    assert upload.status_code == 201, upload.content
    made = upload.json()["exercise"]
    assert (made["name_ar"], made["name_en"], made["name"]) == ("سكوات أمامي", None,
                                                                "سكوات أمامي")
    assert Exercise.objects.filter(pk=made["id"]).values_list("name", flat=True).get() \
        == "سكوات أمامي"
    nameless = api(trainer).post("/api/routine/exercises/create-with-image/", {
        "description": "d", "target_muscle": "Biceps"})
    assert (nameless.status_code, nameless.json()["code"]) == (400, "missing_fields")


def test_b108_the_count_is_fresh_after_a_program_is_generated(make_user, api):
    """`templates/` is served from the response cache, versioned by template writes;
    the count moves on ROUTINE writes, so a trainer who had just generated a client's
    program read the old number for up to five minutes."""
    from routine.models import RoutineTemplate, RoutineTemplateExercise

    trainer, client = _trainer_and_client(make_user, "b108fresh")
    template = RoutineTemplate.objects.create(name="ra b108 fresh", goal="x", days=1,
                                              created_by=trainer)
    RoutineTemplateExercise.objects.create(template=template, day=1, order=1,
                                           exercise=_exercise(trainer, "ra b108 fresh"))
    caller = api(trainer)
    url = f"/api/routine/templates/{template.pk}/"
    assert caller.get(url).json()["used_by_count"] == 0
    made = caller.post(f"/api/routine/templates/{template.pk}/generate/",
                       {"client_id": client.pk}, content_type=JSON)
    assert made.status_code == 201, made.content
    assert caller.get(url).json()["used_by_count"] == 1
    listed = _results(caller.get("/api/routine/templates/my_templates/"))
    assert [t["used_by_count"] for t in listed if t["id"] == template.pk] == [1]


def test_b107_an_empty_equipment_is_not_said(make_user, api):
    trainer = make_user("ra_t_b107empty", user_type="trainer")
    resp = api(trainer).post("/api/routine/exercises/", {
        "name": "Plank", "target_muscle": "Abdominals", "equipment": ""}, content_type=JSON)
    assert resp.status_code == 201, resp.content
    assert (resp.json()["equipment"], resp.json()["equipment_display"]) == (None, None)

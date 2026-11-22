"""Road to 10 S2.11 (the owner's D18, 2026-11-22): body measurements beyond weight.

The app's body page draws four rulers beside weight — chest, arm, waist and thigh, in
centimetres — and refused them itself, because `PerformanceMetric.METRIC_TYPES` knew no
circumference: a POST answered 400 "not a valid choice". These fail before the item.

The ranges are the rulers' own (`yalla_gym` body_models.dart `MeasurementKind`), so the
page cannot offer a value the server then refuses. They are health data: the person's
own, in their export, gone with their account, read by nobody else.
"""
import json
from datetime import timedelta

import pytest
from django.utils import timezone

pytestmark = pytest.mark.django_db

URL = "/api/analytics/metrics/"

#: kind -> (lowest, highest), centimetres: the app's rulers.
RULERS = {
    "chest": (60, 160),
    "arm": (20, 60),
    "waist": (50, 160),
    "thigh": (30, 100),
}


def _post(api, user, **body):
    return api(user).post(URL, data=json.dumps(body), content_type="application/json")


def _row(user, kind, value, days_ago=0, unit="cm"):
    from analytics.models import PerformanceMetric

    row = PerformanceMetric.objects.create(user=user, metric_type=kind, value=value,
                                           unit=unit)
    if days_ago:
        PerformanceMetric.objects.filter(pk=row.pk).update(
            recorded_at=timezone.now() - timedelta(days=days_ago))
        row.refresh_from_db()
    return row


@pytest.mark.parametrize("kind", sorted(RULERS))
def test_a_circumference_is_accepted_across_its_ruler_and_stored_in_centimetres(
        make_user, api, kind):
    from analytics.models import PerformanceMetric

    person = make_user(f"s211_{kind}")
    low, high = RULERS[kind]
    for value in (low, high, (low + high) / 2 + 0.5):
        resp = _post(api, person, metric_type=kind, value=value)
        assert resp.status_code == 201, (kind, value, resp.content)
        body = resp.json()
        assert (body["metric_type"], body["value"], body["unit"]) == (kind, value, "cm")
    stored = PerformanceMetric.objects.filter(user=person, metric_type=kind)
    assert stored.count() == 3
    assert set(stored.values_list("unit", flat=True)) == {"cm"}


@pytest.mark.parametrize("kind", sorted(RULERS))
def test_a_value_off_the_ruler_is_refused_and_nothing_is_stored(make_user, api, kind):
    from analytics.models import PerformanceMetric

    person = make_user(f"s211_off_{kind}")
    low, high = RULERS[kind]
    for value in (low - 0.5, high + 0.5, 0, -10):
        resp = _post(api, person, metric_type=kind, value=value)
        assert resp.status_code == 400, (kind, value, resp.content)
        body = resp.json()
        assert body["code"] == "validation_error"
        assert body["field_errors"]["value"][0]["code"] == "invalid"
    assert not PerformanceMetric.objects.filter(user=person).exists()


def test_the_unit_is_centimetres_whatever_is_sent_and_another_unit_is_refused(
        make_user, api):
    from analytics.models import PerformanceMetric

    person = make_user("s211_unit")
    for sent in ("cm", "CM", " cm ", ""):
        resp = _post(api, person, metric_type="waist", value=82.5, unit=sent)
        assert resp.status_code == 201, (sent, resp.content)
        assert resp.json()["unit"] == "cm"

    for sent in ("in", "inch", "mm", "kg"):
        resp = _post(api, person, metric_type="waist", value=82.5, unit=sent)
        assert resp.status_code == 400, (sent, resp.content)
        body = resp.json()
        assert body["code"] == "validation_error"
        assert body["field_errors"]["unit"][0]["code"] == "unit_not_cm"
    assert PerformanceMetric.objects.filter(user=person).count() == 4


def test_weight_keeps_its_own_range_and_its_unit_is_not_rewritten(make_user, api):
    """The four circumferences are added beside weight; nothing about weight moves."""
    person = make_user("s211_weight")
    resp = _post(api, person, metric_type="weight", value=81.4, unit="kg")
    assert resp.status_code == 201, resp.content
    assert resp.json()["unit"] == "kg"
    # 170 is past every ruler above and a real weight.
    assert _post(api, person, metric_type="weight", value=170).status_code == 201
    assert _post(api, person, metric_type="weight", value=3000).status_code == 400


def test_latest_for_answers_the_newest_row_of_each_named_kind(make_user, api):
    person = make_user("s211_latest")
    other = make_user("s211_latest_other")
    _row(person, "chest", 101.0, days_ago=9)
    chest = _row(person, "chest", 102.5, days_ago=1)
    waist = _row(person, "waist", 84.0, days_ago=3)
    _row(person, "waist", 86.0, days_ago=20)
    weight = _row(person, "weight", 80.0, days_ago=2, unit="kg")
    _row(person, "arm", 36.0, days_ago=4)        # logged, not asked for
    _row(other, "chest", 140.0)                  # somebody else's, and newer
    _row(other, "thigh", 60.0)

    resp = api(person).get(f"{URL}?latest_for=weight,chest,waist,thigh")
    assert resp.status_code == 200, resp.content
    body = resp.json()
    # Newest first, one per kind; `thigh` was never logged by this person, so no row.
    assert [(r["id"], r["metric_type"], r["value"], r["unit"]) for r in body["results"]] == [
        (chest.pk, "chest", 102.5, "cm"),
        (weight.pk, "weight", 80.0, "kg"),
        (waist.pk, "waist", 84.0, "cm"),
    ]
    assert body["count"] == 3


def test_latest_for_is_the_callers_own_even_for_a_staff_account(make_user, api):
    """A staff account's list is the whole table; "my newest chest" is still its own."""
    admin = make_user("s211_admin", user_type="admin")
    _row(make_user("s211_someone"), "chest", 150.0)
    mine = _row(admin, "chest", 99.0, days_ago=5)

    rows = api(admin).get(f"{URL}?latest_for=chest").json()["results"]
    assert [r["id"] for r in rows] == [mine.pk]


def test_latest_for_refuses_a_name_that_is_not_a_metric_type(make_user, api):
    person = make_user("s211_bad")
    _row(person, "chest", 100.0)
    for query in ("latest_for=chest,hips", "latest_for=", "latest_for=,", "latest_for=1"):
        resp = api(person).get(f"{URL}?{query}")
        assert resp.status_code == 400, (query, resp.content)
        body = resp.json()
        assert (body["code"], body["field"]) == ("bad_metric_type", "latest_for")
    # Without the parameter the list is the list it always was.
    assert api(person).get(URL).json()["count"] == 1


def test_the_other_two_reads_answer_the_new_kinds_too(make_user, api):
    from analytics.models import PerformanceMetric

    person = make_user("s211_reads")
    # Through the route, as the app writes them; the first is moved two days back.
    for kind, value in (("thigh", 55.0), ("thigh", 56.5), ("arm", 35.5)):
        assert _post(api, person, metric_type=kind, value=value).status_code == 201
    older, newest = PerformanceMetric.objects.filter(
        user=person, metric_type="thigh").order_by("id")
    PerformanceMetric.objects.filter(pk=older.pk).update(
        recorded_at=timezone.now() - timedelta(days=2))

    current = {r["metric_type"]: r["latest_value"]
               for r in api(person).get(f"{URL}current_metrics/").json()}
    assert current == {"thigh": 56.5, "arm": 35.5}

    trend = api(person).get(f"{URL}metric_trends/?metric_type=thigh&days=30").json()
    assert [row["avg_value"] for row in trend] == [55.0, newest.value]


def test_they_are_the_persons_own_health_data_exported_and_erased_with_the_account(
        make_user, api):
    from analytics.models import PerformanceMetric
    from training_platform.privacy.registry import erase_user_data, export_user_data, registry

    person = make_user("s211_privacy")
    stranger = make_user("s211_privacy_other")
    assert _post(api, person, metric_type="chest", value=104).status_code == 201
    row = PerformanceMetric.objects.get(user=person)

    # Nobody else reads the row: not through the list, not by its id.
    assert api(stranger).get(URL).json()["count"] == 0
    assert api(stranger).get(f"{URL}{row.pk}/").status_code == 404

    source = registry["analytics.PerformanceMetric"]
    assert "health" in source.data_types and source.recipients == ()
    exported = export_user_data(person)["sections"]["performance_metrics"]
    assert [(r["metric_type"], r["value"], r["unit"]) for r in exported] == [
        ("chest", 104.0, "cm")]

    erase_user_data(person)
    assert not PerformanceMetric.objects.filter(pk=row.pk).exists()


def test_the_documents_that_describe_health_data_name_the_measurements():
    """The privacy drafts and the store labels are built from the inventory and must
    stay true (`docs/legal/README.md`): a new kind of health data is named in each."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    inventory = (root / "docs/store/data_inventory.md").read_text(encoding="utf-8")
    assert "chest, arm, waist and thigh" in inventory
    assert "chest, arm, waist and thigh" in (
        root / "docs/legal/privacy_policy_en.md").read_text(encoding="utf-8")
    assert "الصدر والذراع والخصر والفخذ" in (
        root / "docs/legal/privacy_policy_ar.md").read_text(encoding="utf-8")
    assert "chest, arm, waist, thigh" in (
        root / "docs/store/privacy_labels.md").read_text(encoding="utf-8")

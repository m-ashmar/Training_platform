"""Wave-1 review R-5 (the owner, 2026-10-07): a template's goal is the CLOSED list
(`routine.product_models.ProductGoal`: build_muscle, strength, fat_loss, rehab) on
EVERY write, not only through the template route — both operator consoles and the
copy route stored free text — and the home `ready_programs` card carries the catalogue
row's goal code, never the template's raw text.

On 39ae624 a public template whose goal an operator typed as "Hypertrophy" was copied
by another trainer through the API, sold, and shown on the home card as "Hypertrophy"
while `GET /api/v1/products/` answered `build_muscle` for the same product.
"""
import pytest

pytestmark = pytest.mark.django_db

JSON = "application/json"
CODES = ["build_muscle", "strength", "fat_loss", "rehab"]


def _trainer(make_user, tag):
    return make_user(f"w1rg_t_{tag}", user_type="trainer", trainer_is_verified=True)


def _template(trainer, tag, *, public=True):
    """Three training days for four weeks, goal `rehab` (`test_routine_products`)."""
    from tests.test_routine_products import _template as product_template

    template = product_template(trainer, f"w1rg {tag}")
    template.is_public = public
    template.save(update_fields=["is_public", "updated_at"])
    template.refresh_from_db()
    return template


def _stored_before_the_rule(template, text):
    """`template` with older free text as its goal, written past the model the way a
    row stored before the closed list holds it (development data, the owner,
    2026-10-07). `updated_at` does not move, so a product's review still matches it."""
    from routine.models import RoutineTemplate

    RoutineTemplate.objects.filter(pk=template.pk).update(goal=text)
    template.refresh_from_db()
    return template


def _console_forms(make_user):
    """The template form of each console (`/admin/`, `/dj-admin/`), as its pages
    build it."""
    from django.contrib import admin
    from django.test import RequestFactory

    from admin_dashboard.admin import admin_site
    from routine.models import RoutineTemplate

    request = RequestFactory().get("/")
    request.user = make_user("w1rg_operator", user_type="admin")
    return [site._registry[RoutineTemplate].get_form(request)
            for site in (admin.site, admin_site)]


# ------------------------------------------------------------------- the model
@pytest.mark.parametrize("text", ["Hypertrophy", "Strength", "Endurance", "x"])
def test_a_goal_off_the_closed_list_is_refused_on_save(make_user, text):
    """Every ORM write runs the row's rules (`RowValidationMixin`): the model refuses
    free text — and a code in the wrong case, which only the template route
    lower-cases."""
    from django.core.exceptions import ValidationError

    from routine.models import RoutineTemplate

    trainer = _trainer(make_user, "model")
    with pytest.raises(ValidationError) as refused:
        RoutineTemplate.objects.create(name="w1rg free text", goal=text, days=3,
                                       created_by=trainer)
    assert "goal" in refused.value.message_dict
    assert not RoutineTemplate.objects.filter(name="w1rg free text").exists()


def test_each_code_is_stored(make_user):
    from routine.models import RoutineTemplate

    trainer = _trainer(make_user, "codes")
    for code in CODES:
        made = RoutineTemplate.objects.create(name=f"w1rg {code}", goal=code, days=3,
                                              created_by=trainer)
        assert RoutineTemplate.objects.get(pk=made.pk).goal == code


# ------------------------------------------------------------- the two consoles
def test_both_consoles_offer_the_four_codes_and_refuse_free_text(make_user):
    """An operator could type any goal into either console; it reached the catalogue's
    `?goal=` through `goal_code` and the home card raw."""
    trainer = _trainer(make_user, "console")
    template = _template(trainer, "console")
    for form_class in _console_forms(make_user):
        field = form_class.base_fields["goal"]
        offered = [value for value, _label in getattr(field, "choices", [])]
        assert [value for value in offered if value] == CODES, (form_class, offered)

        typed = form_class(data={"goal": "Hypertrophy"}, instance=template)
        typed.is_valid()
        assert typed.errors.get("goal"), (form_class, "free text is refused")

        picked = form_class(data={"goal": "strength"}, instance=template)
        picked.is_valid()
        assert picked.errors.get("goal") is None, (form_class, picked.errors.get("goal"))


# ---------------------------------------------------------------- the copy route
def test_a_copy_stores_the_code_its_source_reads_as(make_user, api):
    """`POST templates/{id}/copy/` wrote the source's text unchecked. A template stored
    before the rule is copied with the code the catalogue reads it as."""
    from routine.models import RoutineTemplate

    author = _trainer(make_user, "copy_author")
    copier = _trainer(make_user, "copy_copier")
    source = _stored_before_the_rule(_template(author, "copy"), "Hypertrophy")

    copied = api(copier).post(f"/api/routine/templates/{source.pk}/copy/")
    assert copied.status_code == 201, copied.content
    assert copied.json()["goal"] == "build_muscle"
    assert RoutineTemplate.objects.get(pk=copied.json()["id"]).goal == "build_muscle"

    # A template already on the list keeps its code.
    own = _template(copier, "copy_own", public=False)
    again = api(copier).post(f"/api/routine/templates/{own.pk}/copy/")
    assert again.status_code == 201, again.content
    assert again.json()["goal"] == "rehab"


def test_a_copy_of_a_goal_that_reads_as_none_is_refused_and_writes_nothing(make_user,
                                                                           api):
    from routine.models import RoutineTemplate, RoutineTemplateExercise

    author = _trainer(make_user, "none_author")
    copier = _trainer(make_user, "none_copier")
    source = _stored_before_the_rule(_template(author, "none"), "Endurance")
    templates = RoutineTemplate.objects.count()
    rows = RoutineTemplateExercise.objects.count()

    refused = api(copier).post(f"/api/routine/templates/{source.pk}/copy/")
    assert refused.status_code == 400, refused.content
    body = refused.json()
    assert body["code"] == "validation_error"
    assert "goal" in body["field_errors"], body
    assert (RoutineTemplate.objects.count(), RoutineTemplateExercise.objects.count()) == \
        (templates, rows)


# ------------------------------------------------------------- the home card
def _sell(make_user, api, trainer, template, tag):
    """Draft, submit and approve a product on `template`, as the trainer and an
    operator do it."""
    from routine import products
    from tests.test_routine_products import _draft

    product = _draft(api, trainer, template)
    submitted = api(trainer).post(f"/api/v1/trainer/products/{product['id']}/submit/")
    assert submitted.status_code == 200, submitted.content
    products.approve(product["id"], make_user(f"w1rg_op_{tag}", user_type="admin"))
    return product["id"]


def _card_and_row(make_user, api, product_id, tag):
    from tests.test_home import _data, _home

    person = make_user(f"w1rg_c_{tag}")
    cards = _data(_home(api, person, HTTP_X_APP_CHANNEL="web"), "ready_programs")
    card = next(c for c in cards["programs"] if c["id"] == product_id)
    listed = api(person).get("/api/v1/products/", HTTP_X_APP_CHANNEL="web")
    assert listed.status_code == 200, listed.content
    row = next(r for r in listed.json()["results"] if r["id"] == product_id)
    return card, row, api(person)


def test_the_card_goal_is_the_catalogue_goal_for_a_template_stored_before_the_rule(
        make_user, api):
    """`home/first_day.py` wrote `template.goal` over the row's code: "Hypertrophy" on
    the card, `build_muscle` on the catalogue, for the same product."""
    trainer = _trainer(make_user, "card")
    template = _template(trainer, "card", public=False)
    product_id = _sell(make_user, api, trainer, template, "card")
    _stored_before_the_rule(template, "Hypertrophy")

    card, row, _reader = _card_and_row(make_user, api, product_id, "card")
    assert row["goal"] == "build_muscle"
    assert card["goal"] == row["goal"]


def test_the_reviewers_path_a_console_goal_copied_and_sold(make_user, api):
    """The review's probe: a public template whose goal an operator typed is copied by
    another trainer through the API, then drafted, submitted and approved. The card,
    the catalogue row and `?goal=` agree on one code, and no key of the card differs
    from the row."""
    from routine.models import RoutineTemplate

    author = _trainer(make_user, "probe_author")
    seller = _trainer(make_user, "probe_seller")
    source = _stored_before_the_rule(_template(author, "probe"), "Hypertrophy")

    copied = api(seller).post(f"/api/routine/templates/{source.pk}/copy/")
    assert copied.status_code == 201, copied.content
    copy = RoutineTemplate.objects.get(pk=copied.json()["id"])
    product_id = _sell(make_user, api, seller, copy, "probe")

    card, row, reader = _card_and_row(make_user, api, product_id, "probe")
    assert (card["goal"], row["goal"]) == ("build_muscle", "build_muscle")
    differs = [key for key, value in row.items() if key != "cover" and card[key] != value]
    assert differs == [], differs
    filtered = reader.get("/api/v1/products/?goal=build_muscle", HTTP_X_APP_CHANNEL="web")
    assert product_id in {r["id"] for r in filtered.json()["results"]}

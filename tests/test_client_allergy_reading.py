"""The client's own read of his allergies answers the server's reading of them
(BACKEND_BUGS B-195; the fix run's wave 2, 2026-11-21).

`GET v1/preferences/` answered `allergies` as the raw text only, while the trainer's
read of the same client (`trainer/clients/{id}/preferences/`, B-128) answered the
server's reading of it: the canonical `allergens` and the `allergy_terms` the vocabulary
does not know, Arabic words included (wave 1). So the Allergies screen re-parsed the
text on the phone to tick its chips: a second parser of one column, in two languages,
that did not know «للوز», «ماعندي» or a free term. The client's read now answers the
same two fields, from the one function both reads call (`diet/allergens.py
allergy_reading`), so the two can never read one text two ways. Additive: `allergies`
and every other field are as they were.

Every test fails on ccd5da0 (the fields and the function did not exist).
"""
import inspect

import pytest

from tests.test_regression_gate import _grant_diet_access

pytestmark = pytest.mark.django_db

MINE = "/api/diet/v1/preferences/"
TRAINERS = "/api/diet/v1/trainer/clients/{id}/preferences/"

#: (what the client typed, the tags the server reads, the words it does not know)
READINGS = [
    ("حساسية للوز", ["tree_nut"], []),
    ("لا يوجد", [], []),
    ("ماعندي", [], []),
    ("حساسية من الفراولة", [], ["الفراولة"]),
    ("حساسية من الكيوي والحليب", ["milk"], ["الكيوي"]),
    ("Sesame, peanuts, kiwi", ["peanut", "sesame"], ["kiwi"]),
    ("nuts", ["peanut", "tree_nut"], []),
    ("none", [], []),
    ("", [], []),
]


def _client(make_user, name):
    client = make_user(name, weight=80, height=180, age=30, gender="male",
                       activity_level="Moderate")
    _grant_diet_access(client)
    return client


def _declare(client, allergies):
    from diet.models import UserFoodPreference

    UserFoodPreference.objects.update_or_create(user=client,
                                                defaults={"allergies": allergies})


def _mine(api, client):
    response = api(client).get(MINE)
    assert response.status_code == 200, response.content[:400]
    return response.json()


@pytest.mark.parametrize("text, allergens, terms", READINGS)
def test_b195_the_clients_own_read_answers_the_servers_reading(make_user, api, text,
                                                                allergens, terms):
    client = _client(make_user, "b195_own")
    _declare(client, text)
    body = _mine(api, client)
    assert body["allergies"] == text                       # as typed, unchanged
    assert (body["allergens"], body["allergy_terms"]) == (allergens, terms), text


def test_b195_the_client_and_his_trainer_read_one_text_the_same_way(make_user, api):
    from users.models import TrainerClientRelation

    trainer = make_user("b195_t", user_type="trainer", trainer_is_verified=True)
    client = _client(make_user, "b195_c")
    TrainerClientRelation.objects.create(trainer=trainer, client=client, status="approved")
    for text, _allergens, _terms in READINGS:
        _declare(client, text)
        own = _mine(api, client)
        theirs = api(trainer).get(TRAINERS.format(id=client.pk))
        assert theirs.status_code == 200, theirs.content[:400]
        theirs = theirs.json()
        assert (own["allergies"], own["allergens"], own["allergy_terms"]) == \
            (theirs["allergies"], theirs["allergens"], theirs["allergy_terms"]), text


def test_b195_a_client_who_never_saved_reads_both_lists_empty(make_user, api):
    from diet.models import UserFoodPreference

    client = _client(make_user, "b195_none")
    UserFoodPreference.objects.filter(user=client).delete()
    body = _mine(api, client)
    assert (body["allergies"], body["allergens"], body["allergy_terms"]) == ("", [], [])
    # The fields beside them are as they were.
    for field in ("liked_foods", "disliked_foods", "protein_choices", "carb_choices",
                  "fat_choices", "vegetable_choices", "fruit_choices", "local_ratio",
                  "medical_disclaimer"):
        assert field in body, field


def test_b195_one_function_answers_both_reads_and_the_docstring_names_the_fields():
    """The two reads call the one function, so they cannot drift; the client's view
    documents what it answers (the docstring is the contract)."""
    from diet.allergens import allergy_reading
    from diet.trainer_reads import TrainerClientPreferencesView
    from diet.views import UserPreferencesView

    assert allergy_reading("حساسية للوز، كيوي") == {"allergens": ["tree_nut"],
                                                    "allergy_terms": ["كيوي"]}
    for view in (UserPreferencesView, TrainerClientPreferencesView):
        assert "allergy_reading(" in inspect.getsource(view.get), view.__name__
    doc = inspect.getdoc(UserPreferencesView)
    for field in ("`allergens`", "`allergy_terms`", "`allergies`"):
        assert field in doc, field

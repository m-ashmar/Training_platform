"""The banned-terms filter on what people write for other people to read (spec B1).

Store blocker B1 asks for "a basic banned-terms filter on post and comment creation";
reporting and blocking already exist (`moderation/`). What is asserted here:

    the list        an operator-edited table (term, language, category, notes, a
                    whole-word flag, on/off, "needs review"), a console page behind the
                    change permission, and a modest seeded starter list marked for review
    the matching    survives case, Arabic letter variants, diacritics, tatweel,
                    invisible characters, presentation forms, repeated letters and
                    letters split by punctuation; whole word or anywhere, per term
    the writes      posts and comments (create AND edit), challenge text, profile names
                    and bio, and the sign-up username answer 400 `content_not_allowed`
                    naming the field, and write nothing. The same on every build.
                    Private chat is deliberately NOT filtered
    the cost        the list is read once and cached; a change reaches the next write;
                    a post write costs the same number of queries at 3 terms and at 300

Every test builds the list it needs: the seeded rows are deleted at the start of each
test (inside its transaction, so they come back) — a test never depends on what a
migration seeded (CLAUDE.md, the `test_zz_*` flush).
"""
import time
import uuid

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

pytestmark = pytest.mark.django_db

JSON = "application/json"
CODE = "content_not_allowed"


# --------------------------------------------------------------------------- setup
def _filter():
    from moderation import ugc_filter

    return ugc_filter


@pytest.fixture(autouse=True)
def an_empty_list(db):
    """Start from no terms and a cold cache; leave a cold cache behind.

    The matcher is cached per process and keyed to a version in the shared cache, which
    outlives a test's rolled-back transaction — so it is invalidated on the way out too,
    or the next test would still see this one's terms.
    """
    from django.apps import apps

    try:
        model = apps.get_model("moderation", "BannedTerm")
    except LookupError:
        model = None  # the code before the filter: every test below fails on its own
    if model is not None:
        model.objects.all().delete()
        _filter().invalidate()
    yield
    if model is not None:
        _filter().invalidate()


def _ban(term, *, whole_word=True, language="en", category="obscenity", **kw):
    from moderation.models import BannedTerm

    return BannedTerm.objects.create(term=term, whole_word=whole_word, language=language,
                                     category=category, **kw)


def _name(tag):
    return f"{tag}_{uuid.uuid4().hex[:6]}"


def _refused(response, field):
    """400, the top-level code, and the field named with the same code."""
    assert response.status_code == 400, response.content
    body = response.json()
    assert body["code"] == CODE, body
    assert body["field_errors"][field][0]["code"] == CODE, body
    assert body["detail"], body
    return body


def _post(api, user, **body):
    return api(user).post("/api/social/posts/", {"content": "hello", **body},
                          content_type=JSON)


# ===================================================================== 1. the list
def test_a_banned_term_is_an_operator_row_with_language_notes_and_a_whole_word_flag():
    row = _ban("Zorblax", whole_word=False, language="en", category="spam",
               notes="seen in a spam wave")
    row.refresh_from_db()
    assert (row.term, row.whole_word, row.language, row.category, row.notes) == (
        "Zorblax", False, "en", "spam", "seen in a spam wave")
    # On by default, and an operator's own row is not "to review".
    assert row.is_active is True
    assert row.needs_review is False
    # Stored folded too, so the console can sort and find duplicates.
    assert row.normalized == "zorblax"


@pytest.mark.parametrize("text", [
    "you are a zorblax",
    "you are a ZORBLAX",                  # case
    "you are a Zorrrrblaxxx",             # repeated letters
    "you are a z.o.r.b.l.a.x",            # letters split by punctuation
    "you are a z-o-r-b-l-a-x!",
    "you are a zor\u200bblax",         # a zero-width space inside the word
    "you are a ｚｏｒｂｌａｘ",             # full-width letters
])
def test_an_english_term_survives_trivial_evasion(text):
    _ban("zorblax")
    hit = _filter().find_banned(text)
    assert hit is not None and hit.term == "zorblax", text


@pytest.mark.parametrize("text", [
    "يا شرموطة",
    "يا شرموطه",                          # ة written ه
    "يا شَرْمُوطَةٌ",                       # diacritics
    "يا شـرمـوطـة",                       # tatweel
    "يا شررررموطة",                       # repeated letters
    "يا ش.ر.م.و.ط.ة",                     # letters split by punctuation
    "يا شر\u200cموطة",                 # a zero-width non-joiner
    "والشرموطة",                          # "and the ..." joined to the word
    "للشرموطة",                           # "for the ..."
    "يا ﺷﺮﻣﻮﻃﺔ",                          # Arabic presentation forms
])
def test_an_arabic_term_survives_letter_variants_marks_and_tatweel(text):
    _ban("شرموطة", language="ar")
    hit = _filter().find_banned(text)
    assert hit is not None, text


@pytest.mark.parametrize("text", ["ازفلوق", "آزفلوق", "إزفلوق", "أزفلوق"])
def test_hamza_forms_of_alef_are_one_letter(text):
    _ban("أزفلوق", language="ar")
    assert _filter().find_banned(f"انت {text} كبير") is not None, text


def test_a_whole_word_term_does_not_match_inside_another_word():
    _ban("zorblax")
    _ban("زفلوق", language="ar")
    find = _filter().find_banned
    assert find("zorblaxian is a planet") is None
    assert find("azorblax") is None
    assert find("زفلوقات") is None
    # ...and a substring term does.
    _ban("grimbo", whole_word=False)
    assert find("megagrimbolic") is not None


def test_a_doubled_letter_in_a_term_means_at_least_two():
    """Repeated letters collapse only down to what the term itself has: a term with a
    double letter does not start matching the everyday word with a single one."""
    _ban("zoss")
    find = _filter().find_banned
    assert find("that zos is fine") is None
    assert find("what a zossss") is not None


def test_an_inactive_term_blocks_nothing():
    _ban("zorblax", is_active=False)
    assert _filter().find_banned("zorblax") is None


def test_clean_text_passes():
    _ban("zorblax")
    _ban("شرموطة", language="ar")
    find = _filter().find_banned
    for text in ("Great session today, new PR on squats!", "تمرين رائع اليوم",
                 "", "   ", "1234", "🔥💪"):
        assert find(text) is None, text


def test_a_long_text_is_matched_quickly():
    """Catastrophic backtracking guard: runs of the separators and repeated letters the
    patterns tolerate must not make a refusal cost seconds."""
    for index in range(150):
        _ban(f"zorb{index:03d}lax", whole_word=bool(index % 2))
    _ban("wa.me/", whole_word=False, category="spam")
    text = ("zzzz" + "." * 5000 + "wa" + "." * 5000 + " ") * 4
    started = time.monotonic()
    assert _filter().find_banned(text) is None
    assert time.monotonic() - started < 1.0


# ------------------------------------------------------------------- the console
def _console(user):
    from django.test import Client

    client = Client()
    client.force_login(user)
    return client


def test_an_operator_edits_the_list_in_the_console_and_the_next_write_obeys(make_user,
                                                                             api):
    from moderation.models import BannedTerm

    operator = make_user(_name("ops"), user_type="admin")
    author = make_user(_name("ath"))
    console = _console(operator)

    assert console.get("/dj-admin/moderation/bannedterm/").status_code == 200
    added = console.post("/dj-admin/moderation/bannedterm/add/", {
        "term": "Zorblax", "whole_word": "on", "language": "en",
        "category": "spam", "notes": "", "is_active": "on"})
    assert added.status_code == 302, added.content
    row = BannedTerm.objects.get(normalized="zorblax")

    # No restart, no wait: the next post is refused.
    _refused(_post(api, author, content="buy zorblax now"), "content")

    changed = console.post(f"/dj-admin/moderation/bannedterm/{row.pk}/change/", {
        "term": "Zorblax", "language": "en", "category": "spam", "notes": ""})
    assert changed.status_code == 302, changed.content  # whole_word and is_active off
    assert _post(api, author, content="buy zorblax now").status_code == 201


def test_a_duplicate_term_is_refused_on_the_form_not_with_a_500(make_user):
    from moderation.models import BannedTerm

    operator = make_user(_name("ops"), user_type="admin")
    _ban("zorblax")
    again = _console(operator).post("/dj-admin/moderation/bannedterm/add/", {
        "term": "ZÓRBLAX", "whole_word": "on", "language": "en",
        "category": "spam", "notes": "", "is_active": "on"})
    assert again.status_code == 200  # the form, with its error
    assert BannedTerm.objects.count() == 1


def test_editing_the_list_needs_the_change_permission(make_user):
    """Operators only (a bare staff account is 403), and changing it needs the change
    permission — an operator without it can read the list and not edit it."""
    from django.contrib.auth.models import Permission

    from moderation.models import BannedTerm

    row = _ban("zorblax")
    staff = make_user(_name("stf"))
    staff.is_staff = True
    staff.save()
    assert _console(staff).get("/dj-admin/moderation/bannedterm/").status_code == 403

    operator = make_user(_name("ops"), user_type="admin")
    operator.is_superuser = False
    operator.save()
    console = _console(operator)
    assert console.get("/dj-admin/moderation/bannedterm/").status_code == 200
    refused = console.post(f"/dj-admin/moderation/bannedterm/{row.pk}/change/", {
        "term": "somethingelse", "language": "en", "category": "spam", "notes": ""})
    assert refused.status_code == 403
    assert console.post("/dj-admin/moderation/bannedterm/add/", {
        "term": "another", "language": "en", "category": "spam",
        "notes": ""}).status_code == 403
    row.refresh_from_db()
    assert row.term == "zorblax" and row.is_active

    operator.user_permissions.add(
        Permission.objects.get(codename="change_bannedterm"),
        Permission.objects.get(codename="add_bannedterm"))
    console = _console(type(operator).objects.get(pk=operator.pk))
    allowed = console.post(f"/dj-admin/moderation/bannedterm/{row.pk}/change/", {
        "term": "zorblax", "language": "en", "category": "spam", "notes": "checked"})
    assert allowed.status_code == 302, allowed.content
    assert BannedTerm.objects.get(pk=row.pk).is_active is False


def test_the_seeded_starter_list_is_modest_bilingual_and_marked_for_review():
    """Run the seed migration's own function; the rows it wrote in the test database
    are not relied on (a `test_zz_*` flush takes them)."""
    import importlib

    from django.apps import apps

    from moderation.models import BannedTerm

    seed = importlib.import_module("moderation.migrations.0004_seed_banned_terms")
    seed.seed_banned_terms(apps, None)
    rows = list(BannedTerm.objects.all())
    assert 20 <= len(rows) <= 120
    assert {"ar", "en"} <= {row.language for row in rows}
    assert {"obscenity", "hate", "spam"} <= {row.category for row in rows}
    assert all(row.needs_review for row in rows)
    # Running it again adds nothing (a migration can be re-applied after a rollback).
    seed.seed_banned_terms(apps, None)
    assert BannedTerm.objects.count() == len(rows)
    _filter().invalidate()
    find = _filter().find_banned
    # A contact-for-sale spam link, whatever follows it.
    assert find("سعر خاص راسلني wa.me/963999999999") is not None
    assert find("Great session today, new PR on squats!") is None
    assert find("تمرين رائع اليوم، رقم قياسي جديد في السكوات") is None


# ==================================================================== 2. the writes
@pytest.mark.parametrize("channel", ["apk", "play", "ios", "web"])
def test_a_post_with_a_banned_term_is_refused_on_every_build_and_nothing_is_written(
        make_user, api, channel):
    from social.models import Post

    _ban("zorblax")
    author = make_user(_name("ath"))
    refused = api(author).post("/api/social/posts/", {"content": "hi ZORBLAX"},
                               content_type=JSON, HTTP_X_APP_CHANNEL=channel,
                               HTTP_X_APP_VERSION="1.0.0")
    _refused(refused, "content")
    assert not Post.objects.filter(author=author).exists()


def test_the_title_of_a_post_is_filtered_too_and_named(make_user, api):
    _ban("zorblax")
    author = make_user(_name("ath"))
    _refused(_post(api, author, title="zorblax", content="fine"), "title")


def test_editing_a_post_into_a_banned_term_is_refused_and_the_post_keeps_its_text(
        make_user, api):
    from social.models import Post

    _ban("zorblax")
    author = make_user(_name("ath"))
    made = _post(api, author, content="clean words")
    assert made.status_code == 201, made.content
    pk = made.json()["id"]
    _refused(api(author).patch(f"/api/social/posts/{pk}/", {"content": "zorblax"},
                               content_type=JSON), "content")
    _refused(api(author).put(f"/api/social/posts/{pk}/",
                             {"content": "ok", "title": "zorblax"},
                             content_type=JSON), "title")
    assert Post.objects.get(pk=pk).content == "clean words"


def test_an_edit_that_does_not_touch_the_text_is_not_refused_for_old_text(make_user,
                                                                          api):
    """Only what the request writes is checked: a term added to the list later does not
    lock an older post against changing its visibility."""
    from social.models import Post

    author = make_user(_name("ath"))
    post = Post.objects.create(author=author, content="old zorblax text")
    _ban("zorblax")
    moved = api(author).patch(f"/api/social/posts/{post.pk}/",
                              {"visibility": "followers"}, content_type=JSON)
    assert moved.status_code == 200, moved.content


def test_a_comment_with_a_banned_term_is_refused_and_counts_nothing(make_user, api):
    from social.models import Comment, Post

    _ban("شرموطة", language="ar")
    author = make_user(_name("ath"))
    commenter = make_user(_name("cmt"))
    post = Post.objects.create(author=author, content="hello")
    refused = api(commenter).post("/api/social/comments/",
                                  {"post": post.pk, "content": "يا شرموطه"},
                                  content_type=JSON)
    _refused(refused, "content")
    assert not Comment.objects.filter(post=post).exists()
    post.refresh_from_db()
    assert post.comments_count == 0


def test_editing_a_comment_into_a_banned_term_is_refused(make_user, api):
    from social.models import Comment, Post

    _ban("zorblax")
    author = make_user(_name("ath"))
    post = Post.objects.create(author=author, content="hello")
    made = api(author).post("/api/social/comments/", {"post": post.pk, "content": "nice"},
                            content_type=JSON)
    assert made.status_code == 201, made.content
    pk = made.json()["id"]
    _refused(api(author).patch(f"/api/social/comments/{pk}/", {"content": "zorblax!"},
                               content_type=JSON), "content")
    assert Comment.objects.get(pk=pk).content == "nice"


def _challenge_body(**over):
    from datetime import timedelta

    from django.utils import timezone

    now = timezone.now()
    return {"title": "Push-up month", "description": "100 a day",
            "challenge_type": "workout", "start_date": (now + timedelta(days=1)).isoformat(),
            "end_date": (now + timedelta(days=30)).isoformat(), **over}


@pytest.mark.parametrize("field", ["title", "description", "rules", "reward_description"])
def test_a_challenge_with_a_banned_term_is_refused(make_user, api, field):
    from social.models import Challenge

    _ban("zorblax")
    creator = make_user(_name("crt"), user_type="trainer")
    refused = api(creator).post("/api/social/challenges/",
                                _challenge_body(**{field: "the zorblax challenge"}),
                                content_type=JSON)
    _refused(refused, field)
    assert not Challenge.objects.filter(creator=creator).exists()


def test_editing_a_challenge_into_a_banned_term_is_refused(make_user, api):
    from social.models import Challenge

    _ban("zorblax")
    creator = make_user(_name("crt"), user_type="trainer")
    made = api(creator).post("/api/social/challenges/", _challenge_body(),
                             content_type=JSON)
    assert made.status_code == 201, made.content
    pk = made.json()["id"]
    _refused(api(creator).patch(f"/api/social/challenges/{pk}/",
                                {"description": "zorblax"}, content_type=JSON),
             "description")
    assert Challenge.objects.get(pk=pk).description == "100 a day"


@pytest.mark.parametrize("field", ["first_name", "last_name"])
def test_a_display_name_with_a_banned_term_is_refused(make_user, api, field):
    _ban("zorblax")
    person = make_user(_name("prs"))
    _refused(api(person).post("/api/auth/user/update/", {field: "Zorblax"},
                              content_type=JSON), field)
    person.refresh_from_db()
    assert getattr(person, field) == ""
    _refused(api(person).post("/api/auth/client/profile/", {field: "Zorblax"},
                              content_type=JSON), field)


def test_a_trainer_bio_and_name_with_a_banned_term_are_refused(make_user, api):
    _ban("zorblax")
    trainer = make_user(_name("trn"), user_type="trainer", trainer_bio="Coach.")
    _refused(api(trainer).post("/api/auth/trainer/profile/",
                               {"trainer_bio": "Best zorblax in town"},
                               content_type=JSON), "trainer_bio")
    _refused(api(trainer).post("/api/auth/user/update/",
                               {"trainer_bio": "zorblax"}, content_type=JSON),
             "trainer_bio")
    _refused(api(trainer).post("/api/auth/trainer/profile/",
                               {"trainer_certifications": ["ACE", "zorblax cert"]},
                               content_type=JSON), "trainer_certifications")
    trainer.refresh_from_db()
    assert trainer.trainer_bio == "Coach."
    # A clean profile write still works.
    fine = api(trainer).post("/api/auth/trainer/profile/", {"trainer_bio": "Coach, 10y."},
                             content_type=JSON)
    assert fine.status_code == 200, fine.content


def test_a_sign_up_username_with_a_banned_term_is_refused_and_no_account_is_made(api):
    from users.models import CustomUser

    _ban("zorblax", whole_word=False)
    username = f"zorblax_{uuid.uuid4().hex[:6]}"
    refused = api().post("/api/auth/register/", {
        "username": username, "email": f"{username}@gate.test",
        "phone_number": "+963999999999", "password1": "Xx!23456long",
        "password2": "Xx!23456long", "user_type": "client",
        "date_of_birth": "1995-06-15"}, content_type=JSON)
    _refused(refused, "username")
    assert not CustomUser.objects.filter(username=username).exists()


def test_private_chat_is_not_filtered(make_user, api):
    """Deliberate: spec B1 asks for a filter on posts and comments. A chat is one
    trainer and one client who chose each other; its safety tools are reporting
    (`coach_message`) and blocking, and its body is stored exactly as typed."""
    from chat.services import open_conversation
    from users.models import TrainerClientRelation

    _ban("zorblax")
    trainer = make_user(_name("trn"), user_type="trainer")
    client = make_user(_name("cli"))
    TrainerClientRelation.objects.create(trainer=trainer, client=client,
                                         status="approved", initiated_by="client")
    conversation = open_conversation(trainer, client)
    sent = api(client).post(f"/api/v1/chat/conversations/{conversation.pk}/messages/",
                            {"body": "zorblax"}, content_type=JSON)
    assert sent.status_code == 201, sent.content


# ======================================================================= 3. the cost
def test_the_list_is_read_once_and_then_served_from_memory():
    _ban("zorblax")
    find = _filter().find_banned
    _filter().invalidate()
    with CaptureQueriesContext(connection) as first:
        assert find("zorblax") is not None
    assert len(first) <= 1
    with CaptureQueriesContext(connection) as later:
        for _ in range(20):
            assert find("zorblax") is not None
            assert find("clean") is None
    assert len(later) == 0


def test_a_change_to_the_list_reaches_the_next_check():
    """Saving and deleting a term invalidate the cache — in this process and, through
    the version in the shared cache, in every other one."""
    find = _filter().find_banned
    assert find("zorblax") is None           # warm, and empty
    row = _ban("zorblax")
    assert find("zorblax") is not None       # added
    row.is_active = False
    row.save()
    assert find("zorblax") is None           # switched off
    row.is_active = True
    row.save()
    assert find("zorblax") is not None
    row.delete()
    assert find("zorblax") is None           # deleted


def test_another_process_bumping_the_version_makes_this_one_reload():
    from moderation.models import BannedTerm

    find = _filter().find_banned
    _ban("zorblax")
    assert find("zorblax") is not None
    # A write that sends no signal (another process, a raw update): this process keeps
    # its copy until the shared version moves...
    BannedTerm.objects.filter(normalized="zorblax").update(is_active=False)
    assert find("zorblax") is not None
    # ...which is what every save and delete in any process does.
    _filter().invalidate()
    with CaptureQueriesContext(connection) as reload:
        assert find("zorblax") is None
    assert len(reload) <= 1


def _post_queries(api, author):
    with CaptureQueriesContext(connection) as ctx:
        made = _post(api, author, content="a clean post about squats")
    assert made.status_code == 201, made.content
    return len(ctx)


def test_a_post_write_costs_the_same_number_of_queries_at_any_list_size(make_user, api):
    author = make_user(_name("ath"))
    for index in range(3):
        _ban(f"zorblax{index}")
    _post(api, author)                       # warm the list and everything else
    small = _post_queries(api, author)

    for index in range(3, 300):
        _ban(f"zorblax{index}", whole_word=bool(index % 2))
    _post(api, author)                       # the list changed: re-read once
    large = _post_queries(api, author)

    assert large == small
    # The whole write, filter included; the filter's own share is zero once warm.
    assert large <= 12

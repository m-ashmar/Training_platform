"""APK distribution, the legal pages and the account-deletion web page (spec B6, B7, §5;
plan step 11, the APK part).

Three things a store listing needs and the app cannot carry itself:

  * **App releases** (`releases/`): what the newest build of each channel is, for the
    in-app update prompt — `GET /api/v1/app/latest`. Only the direct APK is ever handed
    a file: the Play and App Store builds are told the version and nothing to download,
    and a store build cannot ask for the APK (rule zero: by BUILD, never by user).
  * **Legal documents** (`legal/`): the privacy policy and the terms of use, per
    language, versioned, operator-edited, served as public pages at `/legal/privacy/`
    and `/legal/terms/`, and their URLs in the capabilities answer so they change
    without a release (spec B7).
  * **The account-deletion page** (`/legal/delete-account/`): how to delete in the app,
    and a form for someone who cannot open it. It queues a request for an operator; it
    deletes nothing and never says whether an account exists.
"""
import hashlib
import importlib
import re
from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

pytestmark = pytest.mark.django_db

LATEST = "/api/v1/app/latest"
CAPABILITIES = "/api/v1/config/capabilities"
PRIVACY = "/legal/privacy/"
TERMS = "/legal/terms/"
DELETE_ACCOUNT = "/legal/delete-account/"

SHA = "ab" * 32
APK_BYTES = b"PK\x03\x04" + b"yalla gym test apk " * 64


@pytest.fixture(autouse=True)
def media_root(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path / "media")
    return tmp_path


def _release(channel="apk", build=27, **kw):
    from releases.models import AppRelease

    fields = dict(channel=channel, version_name=f"1.{build}.0", build_number=build,
                  min_supported_build=0, release_notes_ar="إصلاحات", release_notes_en="Fixes",
                  is_published=True)
    if channel == "apk" and "apk_file" not in kw:
        fields.update(download_url=f"https://yallagym.example/apk/yalla-{build}.apk",
                      sha256=SHA, size_bytes=4_200_000)
    fields.update(kw)
    release = AppRelease(**fields)
    release.save()
    return release


def _get(client, path, channel="apk", **extra):
    return client.get(path, HTTP_X_APP_CHANNEL=channel, **extra)


# =========================================================================== releases
def test_the_latest_apk_release_is_served_with_its_download(client):
    _release(build=20)
    _release(build=27, min_supported_build=22)

    body = client.get(LATEST, {"channel": "apk", "build": "21"}).json()

    assert body["channel"] == "apk"
    assert body["build"] == 21
    latest = body["latest"]
    assert latest["version_name"] == "1.27.0"
    assert latest["build_number"] == 27
    assert latest["min_supported_build"] == 22
    assert latest["download_url"] == "https://yallagym.example/apk/yalla-27.apk"
    assert latest["sha256"] == SHA
    assert latest["size_bytes"] == 4_200_000
    assert latest["release_notes"] == {"ar": "إصلاحات", "en": "Fixes"}
    assert latest["store_url"] is None
    assert latest["published_at"]
    assert body["update_available"] is True
    assert body["update_required"] is True


@pytest.mark.parametrize("build,available,required", [
    (19, True, True), (25, True, False), (30, False, False), (31, False, False)])
def test_update_available_and_required_compare_the_callers_build(client, build, available,
                                                                  required):
    _release(build=30, min_supported_build=25)
    body = client.get(LATEST, {"channel": "apk", "build": str(build)}).json()
    assert (body["update_available"], body["update_required"]) == (available, required)


def test_without_a_build_nothing_is_compared(client):
    _release(build=30, min_supported_build=25)
    body = _get(client, LATEST, channel="apk").json()
    assert body["build"] is None
    assert body["update_available"] is None and body["update_required"] is None


def test_the_build_is_read_from_the_version_header_of_the_same_channel(client):
    """Flutter reports `1.4.0+27`; the part after `+` is the build number."""
    _release(build=30, min_supported_build=25)
    body = _get(client, LATEST, channel="apk", HTTP_X_APP_VERSION="1.3.0+20").json()
    assert body["build"] == 20 and body["update_required"] is True
    # The hub asking about the APK: its own version says nothing about an APK build.
    body = _get(client, LATEST + "?channel=apk", channel="web",
                HTTP_X_APP_VERSION="1.0.0+99").json()
    assert body["build"] is None


def test_a_draft_or_a_withdrawn_release_is_never_the_latest(client):
    _release(build=27)
    _release(build=40, is_published=False)
    withdrawn = _release(build=35)
    withdrawn.is_published = False
    withdrawn.save()

    latest = client.get(LATEST, {"channel": "apk"}).json()["latest"]
    assert latest["build_number"] == 27


def test_nothing_published_is_a_200_with_no_latest(client):
    body = client.get(LATEST, {"channel": "apk", "build": "3"}).json()
    assert body == {"channel": "apk", "latest": None, "build": 3,
                    "update_available": False, "update_required": False}


@pytest.mark.parametrize("channel", ["play", "ios"])
def test_a_store_channel_is_told_the_version_and_given_nothing_to_download(client, channel):
    store_url = "https://play.google.com/store/apps/details?id=com.yallagym.app"
    _release(channel=channel, build=12, min_supported_build=10, store_url=store_url)

    body = _get(client, LATEST, channel=channel, HTTP_X_APP_VERSION="1.0.0+9").json()
    latest = body["latest"]
    assert latest["build_number"] == 12
    assert latest["download_url"] is None
    assert latest["sha256"] is None and latest["size_bytes"] is None
    assert latest["store_url"] == store_url
    assert body["update_required"] is True


@pytest.mark.parametrize("channel", ["play", "ios"])
def test_a_store_release_cannot_carry_a_file(channel):
    from releases.models import AppRelease

    with pytest.raises(ValidationError) as refused:
        AppRelease(channel=channel, version_name="1.0.0", build_number=1,
                   download_url="https://yallagym.example/x.apk", sha256=SHA,
                   size_bytes=10).save()
    assert "download_url" in refused.value.message_dict


@pytest.mark.parametrize("store", ["play", "ios"])
def test_a_store_build_cannot_ask_for_the_apk(client, store):
    """Rule zero: what a build is told depends on the build. A Play or App Store build
    asking about the direct APK is refused, whoever is signed in."""
    _release(build=27)
    refused = _get(client, LATEST + "?channel=apk", channel=store)
    assert refused.status_code == 400
    assert refused.json()["code"] == "channel_mismatch"
    assert "yallagym.example" not in refused.content.decode()


def test_the_web_hub_may_ask_for_the_apk(client):
    _release(build=27)
    body = _get(client, LATEST + "?channel=apk", channel="web").json()
    assert body["latest"]["download_url"].endswith("yalla-27.apk")


def test_every_refusal_names_its_code(client):
    no_channel = client.get(LATEST, HTTP_X_APP_CHANNEL="")
    assert (no_channel.status_code, no_channel.json()["code"]) == (400, "app_channel_required")

    for bad in ("web", "desktop"):
        refused = client.get(LATEST, {"channel": bad})
        assert (refused.status_code, refused.json()["code"]) == (400, "invalid_choice")

    for bad in ("abc", "-1", "1.5", "99999999999"):
        refused = client.get(LATEST, {"channel": "apk", "build": bad})
        assert (refused.status_code, refused.json()["code"]) == (400, "build_invalid"), bad


def test_every_caller_on_one_build_gets_the_same_answer(client, api, make_user):
    """Two users and a visitor, on one build: byte-identical."""
    _release(build=27, min_supported_build=20)
    query = {"channel": "apk", "build": "21"}
    answers = {
        client.get(LATEST, query).content,
        api(make_user("rel_client")).get(LATEST, query).content,
        api(make_user("rel_trainer", user_type="trainer")).get(LATEST, query).content,
    }
    assert len(answers) == 1


def test_the_answer_is_public_and_keyed_on_the_build(client):
    _release(build=27)
    response = client.get(LATEST, {"channel": "apk"})
    assert response.status_code == 200
    assert "public" in response["Cache-Control"]
    vary = {v.strip() for v in response["Vary"].split(",")}
    assert {"X-App-Channel", "X-App-Version"} <= vary


def test_both_spellings_answer(client):
    _release(build=27)
    for path in (LATEST, LATEST + "/"):
        assert client.get(path, {"channel": "apk"}).status_code == 200


# ------------------------------------------------------------------ uploaded files
def test_an_uploaded_apk_gets_its_checksum_and_size_from_the_server(client):
    upload = SimpleUploadedFile("app-release.apk", APK_BYTES,
                                content_type="application/vnd.android.package-archive")
    release = _release(build=31, apk_file=upload)

    release.refresh_from_db()
    assert release.sha256 == hashlib.sha256(APK_BYTES).hexdigest()
    assert release.size_bytes == len(APK_BYTES)
    assert release.apk_file.name.endswith(".apk")

    latest = client.get(LATEST, {"channel": "apk"}).json()["latest"]
    # A signed media URL, never the bare storage path.
    assert latest["download_url"].startswith("http://testserver/media/")
    assert "?s=" in latest["download_url"]
    assert latest["sha256"] == release.sha256
    downloaded = client.get(latest["download_url"])
    assert downloaded.status_code == 200
    assert b"".join(downloaded.streaming_content) == APK_BYTES
    assert downloaded["Content-Disposition"].startswith("attachment")


def test_an_upload_whose_checksum_the_operator_typed_wrong_is_refused():
    upload = SimpleUploadedFile("app-release.apk", APK_BYTES)
    with pytest.raises(ValidationError) as refused:
        _release(build=32, apk_file=upload, sha256="cd" * 32)
    assert "sha256" in refused.value.message_dict


def test_a_file_that_is_not_an_apk_is_refused():
    upload = SimpleUploadedFile("app-release.apk", b"<html>not an apk</html>")
    with pytest.raises(ValidationError) as refused:
        _release(build=33, apk_file=upload)
    assert "apk_file" in refused.value.message_dict


@pytest.mark.parametrize("fields,field", [
    ({"download_url": "", "sha256": "", "size_bytes": None}, "download_url"),
    ({"download_url": "http://yallagym.example/a.apk"}, "download_url"),
    ({"sha256": "xyz"}, "sha256"),
    ({"size_bytes": None}, "size_bytes"),
    ({"min_supported_build": 40}, "min_supported_build"),
    ({"version_name": "one"}, "version_name"),
])
def test_a_published_apk_release_says_where_its_file_is_and_what_it_is(fields, field):
    with pytest.raises(ValidationError) as refused:
        _release(build=27, **fields)
    assert field in refused.value.message_dict


def test_a_published_release_keeps_its_file_and_build(client):
    release = _release(build=27, min_supported_build=20)

    release.sha256 = "cd" * 32
    with pytest.raises(ValidationError):
        release.save()

    release.refresh_from_db()
    release.min_supported_build = 27
    release.release_notes_en = "Security fix: please update"
    release.save()
    assert client.get(LATEST, {"channel": "apk", "build": "26"}).json()["update_required"]


# ------------------------------------------------------------------------ console
def _operator(make_user, name="rel_operator"):
    user = make_user(name, user_type="admin")
    return user


def test_an_operator_publishes_a_release_in_the_console(client, make_user):
    from releases.models import AppRelease

    operator = _operator(make_user)
    client.force_login(operator)
    assert client.get("/dj-admin/releases/apprelease/").status_code == 200
    response = client.post("/dj-admin/releases/apprelease/add/", {
        "channel": "apk", "version_name": "1.4.0", "build_number": "27",
        "min_supported_build": "20", "release_notes_ar": "جديد", "release_notes_en": "New",
        "download_url": "https://yallagym.example/apk/yalla-1.4.0.apk", "sha256": SHA,
        "size_bytes": "4200000", "store_url": "", "is_published": "on", "_save": "Save",
    })
    assert response.status_code == 302, response.content[:2000]
    release = AppRelease.objects.get(channel="apk", build_number=27)
    assert release.is_published and release.published_at is not None
    assert release.published_by == operator

    # Published: the file and the build are shown, not offered for editing; the
    # minimum supported build still is.
    page = client.get(f"/dj-admin/releases/apprelease/{release.pk}/change/")
    assert page.status_code == 200
    assert b'name="sha256"' not in page.content and b'name="build_number"' not in page.content
    assert b'name="min_supported_build"' in page.content
    # And it is never deleted: withdrawn instead.
    assert client.get(
        f"/dj-admin/releases/apprelease/{release.pk}/delete/").status_code == 403


def test_an_operator_uploads_an_apk_in_the_console(client, make_user):
    from releases.models import AppRelease

    client.force_login(_operator(make_user, "rel_uploader"))
    form = {"channel": "apk", "version_name": "1.5", "build_number": "28",
            "min_supported_build": "0", "release_notes_ar": "", "release_notes_en": "",
            "download_url": "", "sha256": "", "size_bytes": "", "store_url": "",
            "is_published": "on", "_save": "Save"}

    wrong = client.post("/dj-admin/releases/apprelease/add/", {
        **form, "sha256": "cd" * 32,
        "apk_file": SimpleUploadedFile("yalla.apk", APK_BYTES)})
    assert wrong.status_code == 200 and b"does not match the uploaded file" in wrong.content
    assert not AppRelease.objects.filter(build_number=28).exists()

    done = client.post("/dj-admin/releases/apprelease/add/", {
        **form, "apk_file": SimpleUploadedFile("yalla.apk", APK_BYTES)})
    assert done.status_code == 302, done.content[:2000]
    release = AppRelease.objects.get(channel="apk", build_number=28)
    assert release.version_name == "1.5.0"
    assert release.sha256 == hashlib.sha256(APK_BYTES).hexdigest()
    assert release.size_bytes == len(APK_BYTES)
    with release.apk_file.open("rb") as stored:
        assert stored.read() == APK_BYTES


def test_a_staff_account_that_is_not_an_operator_cannot_reach_releases(client, make_user):
    staff = make_user("rel_staff")
    staff.is_staff = True
    staff.save()
    client.force_login(staff)
    assert client.get("/dj-admin/releases/apprelease/").status_code == 403
    assert client.get("/dj-admin/releases/apprelease/add/").status_code == 403


def test_an_operator_without_the_change_permission_can_look_but_not_publish(client,
                                                                          make_user):
    from django.contrib.auth.models import Permission

    operator = make_user("rel_viewer", user_type="admin", is_superuser=False)
    client.force_login(operator)
    assert client.get("/dj-admin/releases/apprelease/").status_code == 200
    assert client.get("/dj-admin/releases/apprelease/add/").status_code == 403

    operator.user_permissions.add(
        Permission.objects.get(codename="add_apprelease"),
        Permission.objects.get(codename="change_apprelease"))
    client.force_login(type(operator).objects.get(pk=operator.pk))
    assert client.get("/dj-admin/releases/apprelease/add/").status_code == 200


# ==================================================================== legal documents
def _document(kind="privacy", language="ar", publish=True, **kw):
    from legal.models import LegalDocument

    document = LegalDocument(kind=kind, language=language,
                             title=kw.pop("title", f"{kind} {language}"),
                             body=kw.pop("body", f"{kind} body {language}"), **kw)
    document.save()
    if publish:
        document.is_published = True
        document.save()
    return document


def test_the_privacy_page_serves_the_newest_published_version_in_the_language_asked(client):
    from legal.models import LegalDocument

    LegalDocument.objects.all().delete()
    _document(body="first arabic text")
    _document(body="second arabic text")
    _document(body="unpublished third text", publish=False)
    _document(language="en", body="the english text")

    arabic = client.get(PRIVACY, {"lang": "ar"})
    assert arabic.status_code == 200
    page = arabic.content.decode()
    assert "second arabic text" in page
    assert "first arabic text" not in page and "unpublished third text" not in page
    assert re.search(r'<html[^>]*lang="ar"[^>]*dir="rtl"', page)
    # The header says what the page is in, whatever the caller's Accept-Language.
    assert arabic["Content-Language"] == "ar"

    english_page = client.get(PRIVACY, {"lang": "en"}, HTTP_ACCEPT_LANGUAGE="ar")
    english = english_page.content.decode()
    assert "the english text" in english
    assert re.search(r'<html[^>]*lang="en"[^>]*dir="ltr"', english)
    assert english_page["Content-Language"] == "en"

    # Arabic is the primary language: no `lang` is Arabic.
    assert "second arabic text" in client.get(PRIVACY).content.decode()


def test_the_legal_pages_are_public_anonymous_and_cacheable(client, api, make_user):
    from legal.models import LegalDocument

    LegalDocument.objects.all().delete()
    _document(kind="terms", language="en", body="terms text")
    anonymous = client.get(TERMS, {"lang": "en"}, HTTP_X_APP_CHANNEL="")
    assert anonymous.status_code == 200
    assert anonymous["Content-Type"].startswith("text/html")
    cache = anonymous["Cache-Control"]
    assert "public" in cache and "max-age=" in cache
    # Nothing a shared cache could hand to the next visitor.
    assert not anonymous.cookies
    # The same bytes for a signed-in user: nothing on the page is about who asks.
    signed_in = api(make_user("legal_reader")).get(TERMS, {"lang": "en"})
    assert _strip_nonce(signed_in.content) == _strip_nonce(anonymous.content)


def _strip_nonce(content):
    return re.sub(rb'nonce="[^"]*"', b'nonce=""', content)


def test_what_the_operator_writes_is_escaped(client):
    from legal.models import LegalDocument

    LegalDocument.objects.all().delete()
    _document(language="en", body="## Heading\n\nHello <script>alert(1)</script>\n\n"
                                   "- one\n- two\n\nWrite to privacy@yallagym.example")
    page = client.get(PRIVACY, {"lang": "en"}).content.decode()
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "<h2>Heading</h2>" in page
    assert "<li>one</li>" in page and "<li>two</li>" in page
    assert 'href="mailto:privacy@yallagym.example"' in page


def test_the_other_language_stands_in_when_one_is_missing(client):
    from legal.models import LegalDocument

    LegalDocument.objects.all().delete()
    _document(kind="terms", language="en", body="only in english")
    page = client.get(TERMS, {"lang": "ar"})
    assert page.status_code == 200
    assert "only in english" in page.content.decode()


def test_a_document_nobody_has_published_is_a_404(client):
    from legal.models import LegalDocument

    LegalDocument.objects.all().delete()
    _document(kind="terms", publish=False)
    assert client.get(TERMS).status_code == 404


def test_a_published_version_is_a_record_and_a_new_version_is_a_new_row():
    first = _document(language="en", body="version one")
    first.body = "rewritten"
    with pytest.raises(ValidationError):
        first.save()

    draft = _document(language="en", body="version two", publish=False)
    assert draft.version == first.version + 1
    assert draft.published_at is None


def test_the_seed_is_an_obvious_draft_in_both_languages(client):
    """The owner writes and approves the real text. Until then the pages must say, on
    their face, that they are a draft."""
    from django.apps import apps

    from legal.models import LegalDocument

    seed = importlib.import_module("legal.migrations.0002_seed_draft_documents")
    LegalDocument.objects.all().delete()
    seed.seed(apps, None)

    rows = LegalDocument.objects.filter(is_published=True)
    assert sorted(rows.values_list("kind", "language")) == [
        ("privacy", "ar"), ("privacy", "en"), ("terms", "ar"), ("terms", "en")]
    for row in rows:
        marker = "مسودة" if row.language == "ar" else "DRAFT"
        assert marker in row.title and marker in row.body
    assert "DRAFT" in client.get(PRIVACY, {"lang": "en"}).content.decode()
    assert "مسودة" in client.get(TERMS, {"lang": "ar"}).content.decode()

    # Running it again adds nothing and changes nothing.
    seed.seed(apps, None)
    assert LegalDocument.objects.count() == 4


def test_an_operator_starts_a_new_draft_from_a_published_version(client, make_user):
    from legal.models import LegalDocument

    LegalDocument.objects.all().delete()
    published = _document(language="en", body="the approved text")
    operator = _operator(make_user, "legal_operator")
    client.force_login(operator)

    assert client.get("/dj-admin/legal/legaldocument/").status_code == 200
    client.post("/dj-admin/legal/legaldocument/", {
        "action": "start_new_draft", "_selected_action": [published.pk]})
    draft = LegalDocument.objects.get(version=published.version + 1)
    assert draft.body == "the approved text" and not draft.is_published

    response = client.post(f"/dj-admin/legal/legaldocument/{draft.pk}/change/", {
        "kind": "privacy", "language": "en", "title": "Privacy policy",
        "body": "the new text", "is_published": "on", "_save": "Save"})
    assert response.status_code == 302, response.content[:2000]
    draft.refresh_from_db()
    assert draft.is_published and draft.published_by == operator
    assert "the new text" in client.get(PRIVACY, {"lang": "en"}).content.decode()

    # A published version is shown read-only and cannot be deleted.
    page = client.get(f"/dj-admin/legal/legaldocument/{draft.pk}/change/")
    assert page.status_code == 200 and b'name="body"' not in page.content
    assert client.get(
        f"/dj-admin/legal/legaldocument/{draft.pk}/delete/").status_code == 403


# ======================================================== capabilities carry the links
@pytest.mark.parametrize("channel", ["apk", "play", "ios", "web"])
def test_the_capabilities_answer_carries_the_legal_links(client, channel):
    links = _get(client, CAPABILITIES, channel=channel).json()["links"]
    assert links == {
        "privacy_policy": {"ar": "http://testserver/legal/privacy/?lang=ar",
                           "en": "http://testserver/legal/privacy/?lang=en"},
        "terms_of_use": {"ar": "http://testserver/legal/terms/?lang=ar",
                         "en": "http://testserver/legal/terms/?lang=en"},
        "account_deletion": {"ar": "http://testserver/legal/delete-account/?lang=ar",
                             "en": "http://testserver/legal/delete-account/?lang=en"},
    }


def test_the_links_follow_the_configured_public_host(client, settings):
    settings.PUBLIC_PAGES_BASE_URL = "https://yallagym.example/"
    links = _get(client, CAPABILITIES).json()["links"]
    assert links["privacy_policy"]["ar"] == "https://yallagym.example/legal/privacy/?lang=ar"
    assert links["account_deletion"]["en"] == (
        "https://yallagym.example/legal/delete-account/?lang=en")


# ================================================================== account deletion
def _ask(client, email, reason="", lang="en", **extra):
    return client.post(DELETE_ACCOUNT, {"email": email, "reason": reason, "lang": lang,
                                        "website": ""}, **extra)


def test_the_deletion_page_explains_the_app_and_offers_the_form(client):
    page = client.get(DELETE_ACCOUNT, {"lang": "en"})
    assert page.status_code == 200
    text = page.content.decode()
    assert "Privacy and data" in text and "Delete my account" in text
    assert re.search(r'<form[^>]*method="post"', text)
    assert 'name="email"' in text and 'name="reason"' in text
    assert "csrfmiddlewaretoken" in text
    # It carries a form token, so it is never stored by a shared cache.
    assert "no-store" in page["Cache-Control"]

    arabic = client.get(DELETE_ACCOUNT).content.decode()
    assert re.search(r'<html[^>]*lang="ar"[^>]*dir="rtl"', arabic)
    assert "الخصوصية والبيانات" in arabic and "احذف حسابي" in arabic


def test_a_request_is_queued_and_the_answer_never_says_whether_an_account_exists(
        client, make_user):
    from legal.models import AccountDeletionRequest
    from users.models import CustomUser

    someone = make_user("del_someone")
    users_before = CustomUser.objects.count()

    known = _ask(client, "  Del_Someone@Gate.TEST ", reason="I moved away")
    unknown = _ask(client, "nobody-here@gate.test")
    assert known.status_code == unknown.status_code == 303
    assert known["Location"] == unknown["Location"]
    assert (_strip_nonce(client.get(known["Location"]).content)
            == _strip_nonce(client.get(unknown["Location"]).content))

    rows = AccountDeletionRequest.objects.order_by("created_at", "id")
    assert [(r.email, r.status) for r in rows] == [
        ("del_someone@gate.test", "open"), ("nobody-here@gate.test", "open")]
    assert rows[0].reason == "I moved away"
    assert rows[0].handled_at is None and rows[0].handled_by is None

    # Nothing was deleted, deactivated or touched.
    someone.refresh_from_db()
    assert someone.is_active and CustomUser.objects.count() == users_before


def test_a_second_open_request_for_the_same_address_is_not_queued_twice(client):
    from legal.models import AccountDeletionRequest

    first = _ask(client, "twice@gate.test")
    second = _ask(client, "TWICE@gate.test")
    assert first["Location"] == second["Location"]
    assert AccountDeletionRequest.objects.filter(email="twice@gate.test").count() == 1


def test_the_form_is_rate_limited(client):
    from legal.models import AccountDeletionRequest
    from legal.views import REQUESTS_PER_HOUR

    for n in range(REQUESTS_PER_HOUR):
        assert _ask(client, f"limit{n}@gate.test").status_code == 303
    refused = _ask(client, "limit-over@gate.test")
    assert refused.status_code == 429
    assert refused["Retry-After"]
    assert AccountDeletionRequest.objects.count() == REQUESTS_PER_HOUR
    # Another address (a different network) is not held back by it.
    assert _ask(client, "elsewhere@gate.test", REMOTE_ADDR="10.9.8.7").status_code == 303


@pytest.mark.parametrize("email", ["", "not-an-email", "a@b", "x" * 250 + "@gate.test"])
def test_an_address_that_is_not_one_is_refused_on_the_page(client, email):
    from legal.models import AccountDeletionRequest

    refused = _ask(client, email)
    assert refused.status_code == 400
    assert 'name="email"' in refused.content.decode()
    assert not AccountDeletionRequest.objects.exists()


def test_a_reason_too_long_is_refused(client):
    from legal.models import AccountDeletionRequest

    assert _ask(client, "long@gate.test", reason="x" * 1001).status_code == 400
    assert not AccountDeletionRequest.objects.exists()


def test_a_filled_honeypot_is_thanked_and_dropped(client):
    from legal.models import AccountDeletionRequest

    bot = client.post(DELETE_ACCOUNT, {"email": "bot@gate.test", "lang": "en",
                                       "website": "http://spam.example"})
    assert bot.status_code == 303
    assert not AccountDeletionRequest.objects.exists()


def test_operators_work_the_queue_in_the_console(client, make_user):
    from legal.models import AccountDeletionRequest

    _ask(client, "queue@gate.test", reason="please")
    request_row = AccountDeletionRequest.objects.get()

    staff = make_user("del_staff")
    staff.is_staff = True
    staff.save()
    client.force_login(staff)
    assert client.get("/dj-admin/legal/accountdeletionrequest/").status_code == 403

    operator = _operator(make_user, "del_operator")
    client.force_login(operator)
    listing = client.get("/dj-admin/legal/accountdeletionrequest/")
    assert listing.status_code == 200 and b"queue@gate.test" in listing.content
    # The operator (only) is told whether an account uses the address.
    holder = make_user("queue")
    page = client.get(f"/dj-admin/legal/accountdeletionrequest/{request_row.pk}/change/")
    assert page.status_code == 200
    assert f"/dj-admin/users/customuser/{holder.pk}/change/".encode() in page.content
    # No one adds a request by hand, and none is deleted from the console.
    assert client.get("/dj-admin/legal/accountdeletionrequest/add/").status_code == 403
    assert client.get(
        f"/dj-admin/legal/accountdeletionrequest/{request_row.pk}/delete/").status_code == 403

    client.post("/dj-admin/legal/accountdeletionrequest/", {
        "action": "mark_no_account", "_selected_action": [request_row.pk]})
    request_row.refresh_from_db()
    assert request_row.status == "no_account"
    assert request_row.handled_by == operator and request_row.handled_at is not None


def test_the_request_is_personal_data_with_a_retention(make_user):
    from training_platform import privacy
    from training_platform.privacy.registry import registry

    from legal.models import AccountDeletionRequest

    source = registry["legal.AccountDeletionRequest"]
    assert source.retention_days and source.retention_field == "handled_at"
    for model in ("releases.AppRelease", "legal.LegalDocument"):
        assert model in registry
    assert privacy.audit_coverage() == []
    assert privacy.validate_sources() == []

    long_ago = timezone.now() - timedelta(days=source.retention_days + 1)
    handled = AccountDeletionRequest.objects.create(
        email="old@gate.test", status="erased", handled_at=long_ago)
    still_open = AccountDeletionRequest.objects.create(email="waiting@gate.test")
    AccountDeletionRequest.objects.filter(pk=still_open.pk).update(created_at=long_ago)
    privacy.purge_expired()
    assert not AccountDeletionRequest.objects.filter(pk=handled.pk).exists()
    assert AccountDeletionRequest.objects.filter(pk=still_open.pk).exists()

    # The requester's own export carries it; their erasure leaves the record of the
    # request and takes their words and address with it.
    person = make_user("del_person")
    mine = AccountDeletionRequest.objects.create(email="del_person@gate.test",
                                                 reason="bye")
    exported = privacy.export_user_data(person)["sections"]
    assert [r["id"] for r in exported[source.label]] == [mine.pk]
    privacy.erase_user_data(person)
    mine.refresh_from_db()
    assert (mine.email, mine.reason) == ("", "")

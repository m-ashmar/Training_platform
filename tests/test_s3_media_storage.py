"""The S3-compatible media backend (`MEDIA_BACKEND=s3`, `training_platform/s3_storage.py`).

It has to promise what the GCS backend promises, because nothing above
`media_storage.media_url()` may know which of them is running:

1. the environment chooses it, and nothing else does;
2. production boots on it only with a bucket, an https address and BOTH keys;
3. a media URL is a signature that expires, and a document URL expires sooner and has
   no public form;
4. an upload is a plain body with no public ACL, which is what every S3-compatible
   store accepts.

Nothing here reaches the network: a signed URL is arithmetic on the secret key, and the
one upload is answered by a hook before it leaves the process. Whether a REAL bucket
accepts the key pair and refuses an unsigned read is `manage.py check_media_storage`,
a deploy step.
"""
import io
import re
import secrets
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from django.core.files.base import ContentFile
from django.core.management import call_command

from tests.test_media_storage import _production_boot

ROOT = Path(__file__).resolve().parent.parent

BUCKET = "yg-media-test"
ENDPOINT = "https://store.objects.test"


@pytest.fixture
def s3_settings(settings, monkeypatch):
    """The settings and the key pair a deploy would have. The pair is GENERATED: a
    key-shaped literal in the repository is what the secret scanners exist to catch."""
    settings.S3_BUCKET_NAME = BUCKET
    settings.S3_ENDPOINT_URL = ENDPOINT
    settings.S3_REGION_NAME = "eu-central"
    settings.S3_LOCATION = ""
    settings.S3_ADDRESSING_STYLE = "virtual"
    settings.VERIFICATION_S3_BUCKET_NAME = ""
    settings.MEDIA_SIGNED_URL_TTL = 3600
    settings.VERIFICATION_DOCUMENT_URL_TTL = 300
    monkeypatch.delenv("AWS_SECRET_NAME", raising=False)
    monkeypatch.setenv("S3_ACCESS_KEY_ID", secrets.token_hex(10).upper())
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", secrets.token_hex(20))
    return settings


def _query(url):
    return {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}


# ===========================================================================
# 1. Chosen by the environment
# ===========================================================================

def test_the_s3_backend_is_chosen_by_the_environment_and_by_nothing_else():
    base = (ROOT / "training_platform" / "settings_base.py").read_text()
    assert "training_platform.s3_storage.SignedS3Storage" in base
    assert "training_platform.s3_storage.PrivateDocumentS3Storage" in base

    module = (ROOT / "training_platform" / "s3_storage.py").read_text()
    # The key pair comes through the secret helpers, and is never written down.
    assert "get_secret" in module
    assert re.search(r"def signed_url", module), "the S3 backend does not sign"

    from django.conf import settings as live
    assert live.MEDIA_BACKEND == "local", "the test suite must not use object storage"


def test_the_backend_names_no_vendor_in_its_settings():
    """The address is a setting, so moving to another S3-compatible store (or to AWS) is
    a change of environment and never of code."""
    module = (ROOT / "training_platform" / "s3_storage.py").read_text()
    code = "\n".join(line for line in module.split('"""', 2)[2].splitlines()
                     if not line.strip().startswith("#"))
    for host in ("your-objectstorage.com", "r2.cloudflarestorage.com"):
        assert host not in code, f"{host} is hard-coded in the backend"


# ===========================================================================
# 2. Production boots on it, and only when it can work
# ===========================================================================

def _s3_boot(**overrides):
    env = dict(MEDIA_BACKEND="s3", S3_BUCKET_NAME=BUCKET, S3_ENDPOINT_URL=ENDPOINT,
               S3_ACCESS_KEY_ID=secrets.token_hex(10).upper(),
               S3_SECRET_ACCESS_KEY=secrets.token_hex(20))
    env.update(overrides)
    return _production_boot(**{key: value for key, value in env.items()
                               if value is not None})


@pytest.mark.django_db(transaction=False)
def test_production_boots_on_an_s3_store():
    booted = _s3_boot()
    assert booted.returncode == 0, booted.stderr
    assert "BOOTED" in booted.stdout


@pytest.mark.django_db(transaction=False)
@pytest.mark.parametrize("missing", ["S3_BUCKET_NAME", "S3_ACCESS_KEY_ID",
                                     "S3_SECRET_ACCESS_KEY"])
def test_production_refuses_an_s3_store_it_cannot_use(missing):
    """A missing bucket stores nothing; a missing half of the key pair signs nothing,
    and every screen with a picture answers 500 on a deploy that booted green."""
    refused = _s3_boot(**{missing: None})
    assert refused.returncode != 0, refused.stdout
    assert missing in refused.stderr
    assert "BOOTED" not in refused.stdout


@pytest.mark.django_db(transaction=False)
def test_production_refuses_a_blank_key_and_a_plain_http_store():
    blank = _s3_boot(S3_SECRET_ACCESS_KEY="   ")
    assert blank.returncode != 0 and "S3_SECRET_ACCESS_KEY" in blank.stderr

    # A signed URL is a bearer token for as long as it lives; over http it, and the
    # member's photo, cross the network in the clear.
    plain = _s3_boot(S3_ENDPOINT_URL="http://store.objects.test")
    assert plain.returncode != 0 and "S3_ENDPOINT_URL" in plain.stderr

    style = _s3_boot(S3_ADDRESSING_STYLE="subdomain")
    assert style.returncode != 0 and "S3_ADDRESSING_STYLE" in style.stderr


@pytest.mark.django_db(transaction=False)
def test_a_public_media_bucket_cannot_also_hold_identity_documents_on_s3():
    """The rule the GCS backend already has: a bucket that serves the app's own pictures
    to everyone serves everything in it, so the documents need a bucket of their own."""
    shared = _s3_boot(MEDIA_PUBLIC_APP_CONTENT="True")
    assert shared.returncode != 0
    assert "VERIFICATION_S3_BUCKET_NAME" in shared.stderr

    apart = _s3_boot(MEDIA_PUBLIC_APP_CONTENT="True",
                     VERIFICATION_S3_BUCKET_NAME="yg-documents-test")
    assert apart.returncode == 0, apart.stderr


@pytest.mark.django_db(transaction=False)
def test_the_gcs_refusals_are_unchanged_and_local_is_still_refused():
    """Adding a second object store must not have opened a third way to boot."""
    for backend in ("local", "filesystem", "s3 "):
        refused = _production_boot(MEDIA_BACKEND=backend)
        assert refused.returncode != 0, backend
    # 's3 ' is stripped to 's3' and then refused for what it lacks, not accepted.
    assert "S3_BUCKET_NAME" in refused.stderr


# ===========================================================================
# 3. URLs: signed, expiring, and never public for a document
# ===========================================================================

def test_a_media_url_is_a_signature_that_expires(s3_settings):
    from training_platform.media_storage import media_url
    from training_platform.s3_storage import SignedS3Storage

    storage = SignedS3Storage()
    url = storage.signed_url("trainers/7/intro.mp4")
    parts, query = urlsplit(url), _query(url)

    assert parts.scheme == "https"
    assert parts.netloc == f"{BUCKET}.store.objects.test"
    assert parts.path == "/trainers/7/intro.mp4"
    assert query["X-Amz-Algorithm"] == "AWS4-HMAC-SHA256"
    assert query["X-Amz-Expires"] == "3600"
    assert re.fullmatch(r"[0-9a-f]{64}", query["X-Amz-Signature"])
    # The key id is in every signed URL by design. The SECRET never is.
    import os
    assert os.environ["S3_SECRET_ACCESS_KEY"] not in url

    # `.url` signs too, so a serializer that forgets `media_url()` still cannot hand out
    # a link that never expires.
    assert "X-Amz-Signature" in storage.url("trainers/7/intro.mp4")

    # One URL can be given a shorter life than the setting.
    assert _query(storage.signed_url("a.jpg", 60))["X-Amz-Expires"] == "60"

    # A different object is a different signature: a URL cannot be edited into another.
    other = _query(storage.signed_url("trainers/8/intro.mp4"))
    assert other["X-Amz-Signature"] != query["X-Amz-Signature"]

    # `media_url()` is the one function callers use, and it finds this backend's
    # signer and its public form by the same names it finds GCS's.
    class Stored:
        name = "catalogue/squat.jpg"
    Stored.storage = storage
    assert "X-Amz-Signature" in media_url(Stored)
    assert _query(media_url(Stored, ttl=120))["X-Amz-Expires"] == "120"
    assert media_url(Stored, public=True) == \
        f"https://{BUCKET}.store.objects.test/catalogue/squat.jpg"


def test_path_style_addresses_the_bucket_in_the_path(s3_settings):
    """Some stores (and any reached by a bare IP or a custom domain) have no
    bucket-named host: `S3_ADDRESSING_STYLE=path` puts the bucket in the path."""
    s3_settings.S3_ADDRESSING_STYLE = "path"
    from training_platform.s3_storage import SignedS3Storage

    storage = SignedS3Storage()
    parts = urlsplit(storage.signed_url("a/b.jpg"))
    assert parts.netloc == "store.objects.test"
    assert parts.path == f"/{BUCKET}/a/b.jpg"
    assert storage.public_url("a/b c.jpg") == f"{ENDPOINT}/{BUCKET}/a/b%20c.jpg"


def test_a_prefix_inside_the_bucket_is_part_of_every_address(s3_settings):
    s3_settings.S3_LOCATION = "staging"
    from training_platform.s3_storage import SignedS3Storage

    storage = SignedS3Storage()
    assert urlsplit(storage.signed_url("a.jpg")).path == "/staging/a.jpg"
    assert storage.public_url("a.jpg").endswith("/staging/a.jpg")


def test_a_verification_document_has_a_five_minute_url_and_no_public_one(s3_settings):
    from training_platform.s3_storage import PrivateDocumentS3Storage

    storage = PrivateDocumentS3Storage()
    url = storage.signed_url("trainer_verification/3/id-card.jpg")
    assert urlsplit(url).path == "/private/trainer_verification/3/id-card.jpg"
    assert _query(url)["X-Amz-Expires"] == "300"
    # The media hour does not leak in through the setting.
    assert _query(storage.url("x.jpg"))["X-Amz-Expires"] == "300"
    with pytest.raises(PermissionError):
        storage.public_url("trainer_verification/3/id-card.jpg")

    # A bucket of their own, when one is named.
    s3_settings.VERIFICATION_S3_BUCKET_NAME = "yg-documents-test"
    apart = PrivateDocumentS3Storage()
    assert urlsplit(apart.signed_url("x.jpg")).netloc == "yg-documents-test.store.objects.test"


def test_the_storage_fails_closed_without_its_bucket_or_its_keys(s3_settings, monkeypatch):
    from django.core.exceptions import ImproperlyConfigured

    from training_platform.s3_storage import SignedS3Storage

    monkeypatch.delenv("S3_SECRET_ACCESS_KEY")
    with pytest.raises(ImproperlyConfigured, match="S3_SECRET_ACCESS_KEY"):
        SignedS3Storage()
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", secrets.token_hex(20))

    s3_settings.S3_BUCKET_NAME = ""
    with pytest.raises(ImproperlyConfigured, match="S3_BUCKET_NAME"):
        SignedS3Storage()
    s3_settings.S3_BUCKET_NAME = BUCKET

    s3_settings.S3_ADDRESSING_STYLE = "subdomain"
    with pytest.raises(ImproperlyConfigured, match="S3_ADDRESSING_STYLE"):
        SignedS3Storage()


def test_the_key_pair_is_the_stores_own_and_never_the_ambient_aws_identity(
        s3_settings, monkeypatch):
    """The process may also carry AWS credentials (the secret store, `AWS_SECRET_NAME`).
    Media must be written with the bucket's key pair and nothing else."""
    from training_platform.s3_storage import SignedS3Storage

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AMBIENTAWSIDENTITY")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", secrets.token_hex(20))
    import os
    url = SignedS3Storage().signed_url("a.jpg")
    credential = _query(url)["X-Amz-Credential"]
    assert credential.startswith(os.environ["S3_ACCESS_KEY_ID"] + "/")
    assert "AMBIENTAWSIDENTITY" not in url


# ===========================================================================
# 4. An upload every S3-compatible store accepts
# ===========================================================================

def test_an_upload_is_a_plain_private_body(s3_settings):
    """Two things a store other than AWS can refuse: the checksum trailer botocore adds
    to every upload since 1.36 (an `aws-chunked` body), and a per-object ACL header on a
    bucket that has ACLs switched off. Neither is sent."""
    from botocore.awsrequest import AWSResponse

    from training_platform.s3_storage import SignedS3Storage

    storage = SignedS3Storage()
    sent = []

    def answer(request, **kwargs):
        sent.append(request)

        class Raw(io.BytesIO):
            def stream(self, *a, **k):
                return iter(())
        return AWSResponse(request.url, 200, {"ETag": '"0"'}, Raw(b""))

    storage.connection.meta.client.meta.events.register("before-send.s3", answer)
    name = storage._save("posts/1/picture.jpg", ContentFile(b"\xff\xd8\xff picture"))

    assert name == "posts/1/picture.jpg"
    puts = [request for request in sent if request.method == "PUT"]
    assert len(puts) == 1, [(request.method, request.url) for request in sent]
    put = puts[0]
    headers = {key.lower(): value.decode() if isinstance(value, bytes) else str(value)
               for key, value in put.headers.items()}
    assert urlsplit(put.url).netloc == f"{BUCKET}.store.objects.test"
    assert urlsplit(put.url).path == "/posts/1/picture.jpg"
    assert "x-amz-acl" not in headers, "an ACL header was sent"
    assert "x-amz-trailer" not in headers, "a checksum trailer was sent"
    assert "aws-chunked" not in headers.get("content-encoding", "")
    assert not any(key.startswith("x-amz-checksum-") or key == "x-amz-sdk-checksum-algorithm"
                   for key in headers), "an optional checksum was sent"
    body = put.body.read() if hasattr(put.body, "read") else put.body
    assert body == b"\xff\xd8\xff picture"
    # Signed with the bucket's key, SigV4.
    assert headers["authorization"].startswith("AWS4-HMAC-SHA256 ")


# ===========================================================================
# 5. The deploy check
# ===========================================================================

class _FakeStore:
    """A storage whose bucket answers as `public` says, with no network."""

    def __init__(self, public):
        self.public, self.objects = public, {}

    def save(self, name, content):
        self.objects[name] = content.read()
        return name

    def signed_url(self, name, ttl=None):
        return f"https://bucket.test/{name}?X-Amz-Signature=abc"

    def delete(self, name):
        self.objects.pop(name, None)

    def exists(self, name):
        return name in self.objects


def _run_check(monkeypatch, settings, public, may_be_public=False):
    from django.core.files import storage as storage_module

    from mediakit.management.commands import check_media_storage as command

    stores = {"default": _FakeStore(public), "verification_documents": _FakeStore(public)}
    settings.MEDIA_BACKEND = "s3"
    settings.MEDIA_PUBLIC_APP_CONTENT = may_be_public
    monkeypatch.setattr(command, "storages", stores)

    class Response:
        def __init__(self, status_code, content=b""):
            self.status_code, self.content = status_code, content

    def get(url, timeout):
        name = urlsplit(url).path.lstrip("/")
        body = next((store.objects[name] for store in stores.values()
                     if name in store.objects), None)
        if "X-Amz-Signature" in url:
            return Response(200, body)
        return Response(200, body) if public else Response(403)

    monkeypatch.setattr(command.requests, "get", get)
    out = io.StringIO()
    call_command("check_media_storage", stdout=out)
    assert not stores["default"].objects and not stores["verification_documents"].objects, \
        "the probe object was left behind"
    return out.getvalue()


def test_the_deploy_check_passes_a_private_store_and_cleans_up(monkeypatch, settings):
    report = _run_check(monkeypatch, settings, public=False)
    assert "FAIL" not in report
    assert report.count("unsigned read is refused") == 2


def test_the_deploy_check_fails_a_public_bucket(monkeypatch, settings):
    """A public bucket is the failure nothing else catches: every picture loads, and so
    does every identity document, for anyone with the address."""
    from django.core.management.base import CommandError

    with pytest.raises(CommandError, match="the bucket is public"):
        _run_check(monkeypatch, settings, public=True)

    # Even when the owner has made the MEDIA bucket public for catalogue pictures, the
    # document storage may never be.
    with pytest.raises(CommandError) as raised:
        _run_check(monkeypatch, settings, public=True, may_be_public=True)
    assert "verification documents" in str(raised.value)
    assert "media: " not in str(raised.value)


def test_the_deploy_check_has_nothing_to_prove_on_the_filesystem(settings):
    settings.MEDIA_BACKEND = "local"
    out = io.StringIO()
    call_command("check_media_storage", stdout=out)
    assert "no object store to check" in out.getvalue()

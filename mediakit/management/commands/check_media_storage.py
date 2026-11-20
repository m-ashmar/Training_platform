"""Prove the media store works, and that it is private, before anyone uploads to it.

A storage that is misconfigured fails quietly and late. A wrong key pair, endpoint or
bucket name boots green (`enforce_production_safety` can check that the settings are
present, not that the store accepts them) and then answers 500 on the first upload. A
bucket created "public" is worse, because nothing fails at all: every picture loads,
and so does every trainer's identity card, for anyone who has or guesses the address.

So this is the check a deploy runs once the settings are in place. For the media
storage and for the verification-document storage it:

1. writes a small object, under a prefix nothing else uses;
2. fetches it through the signed URL the API would hand the app, and compares the bytes;
3. fetches the SAME object with the signature removed, and requires a refusal;
4. deletes it.

    python manage.py check_media_storage          # report, exit 1 on any failure
    python manage.py check_media_storage --quiet  # exit code only

It talks to the real store over the network, so it is a deploy step and never part of
the test suite. On the filesystem backend (development) there is no store to prove
anything about, and it says so.
"""
import secrets
from urllib.parse import urlsplit, urlunsplit

import requests
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import storages
from django.core.management.base import BaseCommand, CommandError

#: Nothing else writes under this prefix, so a probe left behind by a killed run is
#: recognisable for what it is.
PROBE_PREFIX = "healthcheck"
TIMEOUT_SECONDS = 20
#: What a private store answers to a request with no signature. 404 is included because
#: some stores hide the existence of an object from an anonymous caller.
REFUSALS = (401, 403, 404)


def without_signature(url):
    """`url` with its query string removed: the address alone, with no proof attached."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


class Command(BaseCommand):
    help = ("Write, read back and delete a probe object on the media store, and prove "
            "an unsigned read is refused. Exits non-zero if any step fails.")

    def add_arguments(self, parser):
        parser.add_argument("--quiet", action="store_true",
                            help="print nothing; the exit code is the answer")

    def handle(self, *args, **opts):
        self.quiet = opts["quiet"]
        backend = getattr(settings, "MEDIA_BACKEND", "local")
        if backend == "local":
            self._say("MEDIA_BACKEND=local: files are on this machine's disk, and there "
                      "is no object store to check.")
            return

        failures = []
        # The media bucket may be public for the app's own catalogue pictures, and only
        # then (`MEDIA_PUBLIC_APP_CONTENT`); the document storage never may.
        targets = (
            ("default", "media", bool(getattr(settings, "MEDIA_PUBLIC_APP_CONTENT", False))),
            ("verification_documents", "verification documents", False),
        )
        for alias, label, may_be_public in targets:
            self._say(f"{label} ({alias})")
            for step, ok, detail in self._probe(storages[alias], may_be_public):
                self._say(f"  {'ok  ' if ok else 'FAIL'}  {step}{': ' + detail if detail else ''}")
                if not ok:
                    failures.append(f"{label}: {step}: {detail}")

        if failures:
            raise CommandError("the media store is not usable:\n  " + "\n  ".join(failures))
        self._say(self.style.SUCCESS("The media store accepts uploads, signs working "
                                     "URLs and refuses unsigned reads."))

    def _say(self, text):
        if not self.quiet:
            self.stdout.write(text)

    def _probe(self, storage, may_be_public):
        """`[(step, ok, detail)]` for one storage. Never raises: a failure is a row."""
        rows = []
        body = secrets.token_bytes(32)
        try:
            name = storage.save(f"{PROBE_PREFIX}/{secrets.token_hex(16)}.bin",
                                ContentFile(body))
        except Exception as exc:  # the store's own error is the diagnosis
            return [("upload", False, f"{type(exc).__name__}: {exc}")]
        rows.append(("upload", True, ""))

        try:
            signed = storage.signed_url(name) if hasattr(storage, "signed_url") \
                else storage.url(name)
            rows.append(self._signed_read(signed, body))
            rows.append(self._unsigned_read(signed, may_be_public))
        except Exception as exc:
            rows.append(("signed URL", False, f"{type(exc).__name__}: {exc}"))
        finally:
            try:
                storage.delete(name)
                gone = not storage.exists(name)
                rows.append(("delete", gone, "" if gone else "the probe object is still there"))
            except Exception as exc:
                rows.append(("delete", False, f"{type(exc).__name__}: {exc}"))
        return rows

    @staticmethod
    def _signed_read(signed, body):
        step = "signed read"
        try:
            got = requests.get(signed, timeout=TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            return step, False, f"{type(exc).__name__}: {exc}"
        if got.status_code != 200:
            return step, False, f"HTTP {got.status_code}"
        if got.content != body:
            return step, False, "the bytes that came back are not the bytes sent"
        return step, True, ""

    @staticmethod
    def _unsigned_read(signed, may_be_public):
        step = "unsigned read is refused"
        try:
            bare = requests.get(without_signature(signed), timeout=TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            return step, False, f"{type(exc).__name__}: {exc}"
        if bare.status_code in REFUSALS:
            return step, True, f"HTTP {bare.status_code}"
        if bare.status_code == 200 and may_be_public:
            return step, True, "served, as MEDIA_PUBLIC_APP_CONTENT says this bucket is public"
        if bare.status_code == 200:
            return (step, False,
                    "the object was SERVED with no signature: the bucket is public. "
                    "Make it private before anything is uploaded")
        return step, False, f"unexpected HTTP {bare.status_code}"

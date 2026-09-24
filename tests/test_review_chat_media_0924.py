"""The chat media route, after the adversarial review of 2026-09-24 (B-091's route).

One section per confirmed finding; each test fails on the code the review read:

    1  a refused photo was QUARANTINED     the first 64 KB of a private photo, EXIF and
                                           GPS included, kept under quarantine/ where no
                                           erasure reaches. Chat now refuses without a
                                           sample, and an oversize photo is refused before
                                           the validator runs at all
    2  the FileField switched off fast      Message had a generic post_delete receiver, so
       delete                               every erasure loaded every message of every
                                           thread into memory. The files now go through
                                           an explicit sweep, and the rows stay a DELETE
    3  no storage bound                     a per-sender rolling-24 h count and byte quota,
                                           decided before a byte is decoded or stored, and
                                           a voice note's own byte cap
    4  a truncated photo answered 500       the image guard turns a failed decode into
                                           `unreadable_image`, on every route
    5  `invalid_client_uuid` never reached   the top-level `code`, on the text route, the
       the top-level `code`                 media route and the socket
    6  the refusal table did not match      the docstring and API_CONTRACT.md list what the
       the route                            route really answers, and nothing it cannot

Uploads are bytes assembled here, as in `tests/test_chat_media.py`: the guard reads the
bytes, so a file from disk would prove less.
"""
import io
import json
import os
import re
import struct
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from asgiref.sync import async_to_sync
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext, override_settings
from django.utils import timezone
from PIL import Image

pytestmark = pytest.mark.django_db

ROOT = Path(__file__).resolve().parents[1]
CHAT = '/api/v1/chat'
MEDIA = f'{CHAT}/media/'


# --------------------------------------------------------------------------- setup
@pytest.fixture(autouse=True)
def media_root(settings, tmp_path):
    """Uploads — and quarantine samples — land in a directory of this test's own."""
    settings.MEDIA_ROOT = str(tmp_path)
    settings.MEDIA_URL_SIGNING = True
    settings.QUARANTINE_LOCATION = 'quarantine'
    return tmp_path


@pytest.fixture
def pair(make_user):
    from users.models import TrainerClientRelation

    trainer = make_user("rcm_trainer", user_type="trainer")
    client = make_user("rcm_client", user_type="client")
    TrainerClientRelation.objects.create(trainer=trainer, client=client,
                                         status="approved", initiated_by="client")
    return trainer, client


@pytest.fixture
def conversation(pair):
    from chat.services import open_conversation

    return open_conversation(*pair)


def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload) + 8) + kind + payload


def _mvhd(seconds: float, timescale: int = 1000) -> bytes:
    body = (b"\x00\x00\x00\x00" + struct.pack(">I", 0) + struct.pack(">I", 0)
            + struct.pack(">I", timescale) + struct.pack(">I", int(seconds * timescale))
            + b"\x00\x01\x00\x00" + b"\x01\x00" + b"\x00" * 10 + b"\x00" * 36
            + b"\x00" * 24 + struct.pack(">I", 2))
    return _box(b"mvhd", body)


def iso_bytes(seconds=4.0, brand=b"isom", padding=2048) -> bytes:
    return (_box(b"ftyp", brand + struct.pack(">I", 512) + b"isom" + b"mp42")
            + _box(b"moov", _mvhd(seconds)) + _box(b"mdat", b"\x00" * padding))


def video(seconds=4.0, name="clip.mp4", padding=2048):
    return SimpleUploadedFile(name, iso_bytes(seconds, padding=padding),
                              content_type="video/mp4")


def voice(seconds=12.0, name="note.m4a", padding=2048):
    return SimpleUploadedFile(name, iso_bytes(seconds, brand=b"M4A ", padding=padding),
                              content_type="audio/mp4")


def jpeg_with_gps(w=900, h=1200, noise=False) -> bytes:
    """A JPEG carrying EXIF with a maker string and a GPS block, as a phone writes."""
    if noise:
        image = Image.frombytes("RGB", (w, h), os.urandom(w * h * 3))
    else:
        image = Image.new("RGB", (w, h), (120, 80, 40))
    exif = Image.Exif()
    exif[0x010F] = "PhoneMakerXYZ"                   # Make
    exif[0x8825] = {1: "N", 2: (33.0, 30.0, 0.0)}    # GPSInfo
    buf = io.BytesIO()
    image.save(buf, format="JPEG", exif=exif, quality=90)
    return buf.getvalue()


def photo(w=900, h=1200, name="IMG_4242.jpg"):
    return SimpleUploadedFile(name, jpeg_with_gps(w, h), content_type="image/jpeg")


def _post(api, user, conversation, kind, file, **extra):
    data = {'conversation': conversation.pk, 'kind': kind, **extra}
    if file is not None:
        data['file'] = file
    return api(user).post(MEDIA, data)


def _files_under(root, folder):
    base = os.path.join(str(root), folder)
    found = []
    for here, _dirs, names in os.walk(base):
        found += [os.path.join(here, name) for name in names]
    return found


def _stored(root):
    return _files_under(root, "chat")


def _quarantined(root):
    return _files_under(root, "quarantine")


# ======================================================== 1  refused, not quarantined
def test_an_oversize_chat_photo_is_refused_and_nothing_of_it_is_kept(
        api, pair, conversation, media_root):
    """The most common refusal a phone photo meets: over 5 MB. It was refused INSIDE
    `validate_file`, which wrote its first 64 KB — the EXIF block, the maker, the GPS
    tag — to `quarantine/quarantine_IMG_4242.jpg_<ts>`, outside the thread and outside
    every erasure. Now the size is checked before the validator runs (`max_bytes`, as
    the profile-picture route does), and chat never quarantines anyway."""
    _trainer, client = pair
    head = jpeg_with_gps()
    big = SimpleUploadedFile("IMG_4242.jpg", head + b"\x00" * (5 * 1024 * 1024 + 1024),
                             content_type="image/jpeg")
    refused = _post(api, client, conversation, 'image', big)
    assert refused.status_code == 400, refused.content
    assert refused.json()['code'] == 'file_too_large'
    assert not _quarantined(media_root), (
        "a refused private photo was copied into quarantine/ with its EXIF and GPS")
    assert not _stored(media_root)


@pytest.mark.parametrize("upload, code", [
    # A panorama: 11:1, over the guard's 10:1.
    (lambda: photo(2200, 200), 'invalid_aspect_ratio'),
    # An iPhone's default format: HEIC is not one the guard takes.
    (lambda: SimpleUploadedFile("IMG_0002.heic",
                                b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic"
                                + b"PhoneMakerXYZ" + b"\x00" * 4000,
                                content_type="image/heic"), 'invalid_mime_type'),
], ids=["panorama", "heic"])
def test_no_refused_chat_photo_is_quarantined(api, pair, conversation, media_root,
                                              upload, code):
    _trainer, client = pair
    refused = _post(api, client, conversation, 'image', upload())
    assert refused.status_code == 400, refused.content
    assert refused.json()['code'] == code
    assert not _quarantined(media_root), f"a chat photo refused as {code} was quarantined"


def test_every_other_route_still_quarantines_what_it_refuses(media_root):
    """`quarantine=False` is chat's choice; the default is unchanged, so the profile
    picture, the routine images and every serializer upload keep their sample."""
    from django.core.exceptions import ValidationError

    from training_platform.file_security import (FileSecurityValidator,
                                                 process_uploaded_image)

    with pytest.raises(ValidationError) as refused:
        process_uploaded_image(photo(2200, 200, name="pano.jpg"))
    assert refused.value.code == 'invalid_aspect_ratio'
    assert len(_quarantined(media_root)) == 1, "the default path stopped quarantining"

    with pytest.raises(ValidationError):
        process_uploaded_image(photo(2200, 200, name="pano2.jpg"), quarantine=False)
    with pytest.raises(ValidationError):
        FileSecurityValidator().validate_file(photo(2200, 200, name="pano3.jpg"),
                                              'image', quarantine=False)
    assert len(_quarantined(media_root)) == 1, "quarantine=False still wrote a sample"


# ======================================================== 2  fast delete, files still go
def test_a_message_can_be_deleted_without_loading_it():
    """`Collector.can_fast_delete` is False for any model with a delete receiver. The
    generic file receiver made Message one: an erasure then collected every message of
    every thread as an instance and ran a receiver per row (the review measured 19.5 MB
    and 2.85 s for 20,000 text messages, linear)."""
    from django.db.models.deletion import Collector

    from chat.models import Message

    assert Collector(using='default', origin=None).can_fast_delete(
        Message.objects.all()), "chat.Message lost the fast delete path"


def test_deleting_a_conversation_takes_its_files_and_loads_no_message(
        api, pair, conversation, media_root, django_capture_on_commit_callbacks):
    from chat.models import Message

    trainer, client = pair
    assert _post(api, client, conversation, 'image', photo()).status_code == 201
    assert _post(api, trainer, conversation, 'voice', voice()).status_code == 201
    assert _post(api, client, conversation, 'video', video()).status_code == 201
    Message.objects.bulk_create(
        Message(conversation=conversation, sender=client, body=f"text {n}")
        for n in range(40))
    assert len(_stored(media_root)) == 3

    with CaptureQueriesContext(connection) as queries, \
            django_capture_on_commit_callbacks(execute=True):
        conversation.delete()

    loaded = [q['sql'] for q in queries.captured_queries
              if '"chat_message"."body"' in q['sql']]
    assert not loaded, f"the delete read message rows into memory: {loaded[:1]}"
    assert not Message.objects.exists()
    assert not _stored(media_root), "the conversation's files outlived it"


def test_a_delete_that_rolls_back_keeps_the_files(api, pair, conversation, media_root,
                                                  django_capture_on_commit_callbacks):
    from chat.models import Message

    _trainer, client = pair
    assert _post(api, client, conversation, 'video', video()).status_code == 201
    thread = conversation.pk        # `delete()` clears the instance's pk, rolled back or not

    with django_capture_on_commit_callbacks(execute=True):
        with pytest.raises(RuntimeError):
            with transaction.atomic():
                conversation.delete()
                raise RuntimeError("roll it back")

    assert Message.objects.filter(conversation_id=thread).exists()
    assert len(_stored(media_root)) == 1, "a rolled-back delete removed the file"


def test_erasure_takes_every_file_once_and_its_dry_run_takes_none(
        api, pair, conversation, media_root, django_capture_on_commit_callbacks,
        monkeypatch):
    """Both sides' files go with a joint thread. The erasure preview counts the files
    the person sent; the storage is asked to delete each name exactly once, though two
    steps (the conversation's receiver and the messages source) both reach it."""
    from django.core.files.storage import FileSystemStorage

    from training_platform.privacy.registry import erase_user_data

    trainer, client = pair
    assert _post(api, client, conversation, 'image', photo()).status_code == 201
    assert _post(api, trainer, conversation, 'voice', voice()).status_code == 201
    assert len(_stored(media_root)) == 2

    preview = erase_user_data(client, dry_run=True)
    assert preview['messages_sent']['files'] == 1, preview['messages_sent']
    assert len(_stored(media_root)) == 2, "a dry run deleted a file"

    deleted = []
    real_delete = FileSystemStorage.delete

    def counting_delete(self, name):
        deleted.append(name)
        return real_delete(self, name)

    monkeypatch.setattr(FileSystemStorage, 'delete', counting_delete)
    with django_capture_on_commit_callbacks(execute=True):
        erase_user_data(client)
    assert not _stored(media_root), "erasure left the chat's files in storage"
    chat_deletes = [name for name in deleted if name.startswith('chat/')]
    assert len(chat_deletes) == len(set(chat_deletes)) == 2, chat_deletes


def test_a_merged_sweep_never_outlives_a_rollback(media_root,
                                                 django_capture_on_commit_callbacks):
    """`delete_files_on_commit` folds sweeps of one transaction into one callback. A
    name scheduled inside a savepoint that rolls back must stay, and one scheduled
    outside it must go, merge or no merge."""
    from django.core.files.base import ContentFile
    from django.core.files.storage import default_storage

    from chat.media import delete_files_on_commit

    names = {key: default_storage.save(f"chat/0/{key}.bin", ContentFile(b"x"))
             for key in ("kept_outer", "outer", "inner", "after")}
    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        with transaction.atomic():
            delete_files_on_commit([names["outer"]])
            with pytest.raises(RuntimeError):
                with transaction.atomic():
                    delete_files_on_commit([names["inner"]])
                    raise RuntimeError("roll the inner savepoint back")
            delete_files_on_commit([names["after"], names["outer"]])
    assert len(callbacks) == 1, "two sweeps at one depth were not merged"
    assert not default_storage.exists(names["outer"])
    assert not default_storage.exists(names["after"])
    assert default_storage.exists(names["inner"]), "a rolled-back name was deleted"
    assert default_storage.exists(names["kept_outer"])


def test_erasure_takes_a_sent_file_the_conversation_receiver_cannot_reach(
        make_user, pair, conversation, media_root, django_capture_on_commit_callbacks):
    """The messages source deletes by SENDER, as a plain DELETE. A row it deletes whose
    thread survives the erasure has no receiver to take its file, so the source's own
    `before_erase` does. (Such a row is outside what the services write — a sender is
    always one of the two — which is exactly why it must not depend on them.)"""
    from django.core.files.base import ContentFile

    from chat.models import KIND_IMAGE, Message
    from chat.services import open_conversation
    from training_platform.privacy.registry import erase_user_data

    trainer, client = pair
    other_trainer = make_user("rcm_trainer2", user_type="trainer")
    other_client = make_user("rcm_client2", user_type="client")
    elsewhere = open_conversation(other_trainer, other_client)
    stray = Message(conversation=elsewhere, sender=client, kind=KIND_IMAGE)
    stray.attachment.save("image.jpg", ContentFile(jpeg_with_gps(40, 40)), save=False)
    stray.save()
    assert len(_stored(media_root)) == 1

    with django_capture_on_commit_callbacks(execute=True):
        erase_user_data(client)
    assert not Message.objects.filter(pk=stray.pk).exists()
    assert not _stored(media_root), "the sender's file outlived the erased row"


# ======================================================== 3  the quota and the voice cap
def test_the_owners_numbers_are_the_defaults():
    from django.conf import settings

    assert settings.CHAT_MEDIA_DAILY_COUNT == 60
    assert settings.CHAT_MEDIA_DAILY_BYTES == 512 * 1024 * 1024
    assert settings.CHAT_VOICE_MAX_BYTES == 10 * 1024 * 1024


def test_the_daily_count_is_refused_before_the_file_is_read(api, pair, conversation,
                                                           media_root, settings):
    from chat.models import Message

    _trainer, client = pair
    settings.CHAT_MEDIA_DAILY_COUNT = 2
    key = str(uuid.uuid4())
    first = _post(api, client, conversation, 'voice', voice(), client_uuid=key)
    assert first.status_code == 201, first.content
    assert _post(api, client, conversation, 'video', video()).status_code == 201

    # Garbage bytes: had the guard run first, this would be a 400. It is a 429, so the
    # quota was decided before a byte of the upload was decoded or stored.
    garbage = SimpleUploadedFile("x.jpg", b"\x00" * 5000, content_type="image/jpeg")
    refused = _post(api, client, conversation, 'image', garbage)
    assert refused.status_code == 429, refused.content
    body = refused.json()
    assert body['code'] == 'media_quota_exceeded'
    assert 0 < body['retry_after'] <= 24 * 3600
    assert body['retry_after_seconds'] == body['retry_after']
    assert refused['Retry-After'] == str(body['retry_after'])
    assert Message.objects.count() == 2
    assert len(_stored(media_root)) == 2

    # A retry of a message that DID land is answered, not refused: it stores nothing.
    replay = _post(api, client, conversation, 'voice', voice(), client_uuid=key)
    assert replay.status_code == 200 and replay.json()['id'] == first.json()['id']
    # Text is not media and never counts.
    text = api(client).post(f"{CHAT}/conversations/{conversation.pk}/messages/",
                            {'body': 'still here'}, content_type="application/json")
    assert text.status_code == 201


def test_the_daily_bytes_count_what_is_kept_including_taken_back_media(
        api, pair, conversation, media_root, settings):
    from chat.models import Message

    trainer, client = pair
    one = len(iso_bytes(brand=b"M4A "))
    settings.CHAT_MEDIA_DAILY_BYTES = one + 100
    sent = _post(api, client, conversation, 'voice', voice())
    assert sent.status_code == 201
    row = Message.objects.get(pk=sent.json()['id'])
    [kept] = _stored(media_root)
    assert row.size_bytes == os.path.getsize(kept) == one

    # Taking it back keeps the file (the Message docstring), so it still counts.
    assert api(client).delete(f"{CHAT}/messages/{row.pk}/").status_code == 200
    refused = _post(api, client, conversation, 'voice', voice())
    assert refused.status_code == 429 and refused.json()['code'] == 'media_quota_exceeded'

    # The window is per SENDER: the trainer's allowance is untouched.
    assert _post(api, trainer, conversation, 'voice', voice()).status_code == 201

    # And it rolls: 25 hours later that note no longer counts.
    Message.objects.filter(pk=row.pk).update(created_at=timezone.now() - timedelta(hours=25))
    assert _post(api, client, conversation, 'voice', voice()).status_code == 201


def test_a_photo_counts_the_bytes_that_were_stored(api, pair, conversation, media_root):
    from chat.models import Message

    _trainer, client = pair
    sent = _post(api, client, conversation, 'image', photo())
    assert sent.status_code == 201
    [kept] = _stored(media_root)
    assert Message.objects.get(pk=sent.json()['id']).size_bytes == os.path.getsize(kept)


def test_a_voice_note_has_its_own_byte_cap(api, pair, conversation, settings, media_root):
    _trainer, client = pair
    settings.CHAT_VOICE_MAX_BYTES = 1024
    too_big = _post(api, client, conversation, 'voice', voice())
    assert too_big.status_code == 400, too_big.content
    assert too_big.json()['code'] == 'media_audio_too_large'
    # The same number of bytes as a video is well inside the video cap.
    assert _post(api, client, conversation, 'video', video()).status_code == 201


# ======================================================== 4  a truncated photo
def _truncated_jpeg():
    whole = jpeg_with_gps(900, 1200, noise=True)
    return whole[:len(whole) // 2]


def test_a_truncated_photo_is_a_400_not_a_500(api, pair, conversation, media_root):
    """`verify()` does not decode JPEG scan data, so a half-downloaded gallery photo
    passed the validator and `img.load()` raised OSError('image file is truncated')
    straight through the view: 500 `internal_server_error`."""
    _trainer, client = pair
    cut = SimpleUploadedFile("IMG_half.jpg", _truncated_jpeg(), content_type="image/jpeg")
    refused = _post(api, client, conversation, 'image', cut)
    assert refused.status_code == 400, refused.content
    assert refused.json()['code'] == 'unreadable_image'
    assert not _stored(media_root)


def test_the_image_guard_refuses_a_truncated_photo_on_every_route():
    from django.core.exceptions import ValidationError

    from training_platform.file_security import process_uploaded_image

    cut = SimpleUploadedFile("half.jpg", _truncated_jpeg(), content_type="image/jpeg")
    with pytest.raises(ValidationError) as refused:
        process_uploaded_image(cut, max_bytes=5 * 1024 * 1024)
    assert refused.value.code == 'unreadable_image'


# ======================================================== 5  invalid_client_uuid
def test_a_malformed_client_uuid_is_the_top_level_code_on_both_routes(
        api, pair, conversation):
    _trainer, client = pair
    text = api(client).post(f"{CHAT}/conversations/{conversation.pk}/messages/",
                            {'body': 'hi', 'client_uuid': 'nope'},
                            content_type="application/json")
    media = _post(api, client, conversation, 'voice', voice(), client_uuid='nope')
    for answer in (text, media):
        assert answer.status_code == 400, answer.content
        body = answer.json()
        assert body['code'] == 'invalid_client_uuid', body
        assert body['field_errors']['client_uuid'][0]['code'] == 'invalid_client_uuid'


@pytest.fixture
def socket_keeps_the_test_connection(monkeypatch):
    """See `tests/test_chat.py`: stop Channels closing the test's own connection."""
    monkeypatch.setattr('channels.db.close_old_connections', lambda: None)


@override_settings(CHANNEL_LAYERS={
    'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}})
def test_a_malformed_client_uuid_is_the_same_code_on_the_socket(
        pair, conversation, socket_keeps_the_test_connection):
    from channels.routing import URLRouter
    from channels.testing import WebsocketCommunicator
    from rest_framework_simplejwt.tokens import RefreshToken

    import chat.routing

    _trainer, client = pair
    application = URLRouter(chat.routing.websocket_urlpatterns)
    token = str(RefreshToken.for_user(client).access_token)

    async def scenario():
        comm = WebsocketCommunicator(
            application, f"/ws/chat/{conversation.pk}/?token={token}")
        connected, _sub = await comm.connect()
        assert connected
        await comm.send_to(text_data=json.dumps(
            {'type': 'message.send', 'body': 'hi', 'client_uuid': 'nope'}))
        frame = json.loads(await comm.receive_from(timeout=5))
        await comm.disconnect()
        return frame

    frame = async_to_sync(scenario)()
    assert frame['type'] == 'error' and frame['code'] == 'invalid_client_uuid', frame


# ======================================================== 6  the table is the route
#: Every `code` POST /api/v1/chat/media/ can answer. Each is produced by a request in
#: this file or in tests/test_chat_media.py, or is the guard failing on its own
#: (`mime_detection_failed`, `file_validation_error`).
REACHABLE = {
    # before the view: the body cap, the hourly allowance, authentication, the parser
    'length_required', 'payload_too_large', 'rate_limited', 'not_authenticated',
    'token_not_valid', 'unsupported_media_type', 'parse_error',
    # the send, in order
    'not_found', 'idempotency_conflict', 'invalid_client_uuid', 'validation_error',
    'exercise_only_on_video', 'unknown_exercise', 'no_relation', 'blocked',
    'coaching_frozen', 'coaching_unpaid', 'coaching_ended', 'media_quota_exceeded',
    'media_file_required',
    'media_not_a_video', 'media_video_too_large', 'media_video_too_long',
    'media_not_audio', 'media_audio_too_large', 'media_audio_too_long', 'empty_file',
    'file_too_large', 'dangerous_extension', 'invalid_mime_type', 'corrupt_image',
    'invalid_aspect_ratio', 'unreadable_image', 'unsupported_format',
    'image_dimensions_too_large', 'image_too_many_pixels', 'mime_detection_failed',
    'file_validation_error',
}
#: What the image guard can say elsewhere and never here: the file is never None
#: (`media_file_required` first), Django's multipart parser drops a part with no file
#: name, chat always asks for an image, and chat skips the byte-pattern scan.
UNREACHABLE = {'no_file', 'no_filename', 'invalid_file_object', 'unknown_file_type',
               'malicious_content', 'executable_file', 'invalid_media'}


def _table_codes():
    from chat.views import MediaMessageView

    rows = [line for line in MediaMessageView.__doc__.splitlines()
            if line.strip().startswith('|') and not set(line.strip()) <= set('|- ')]
    codes = set()
    for row in rows[1:]:                    # the header row names columns, not codes
        cells = row.strip().strip('|').split('|')
        codes |= set(re.findall(r'`([a-z_]+)`', cells[1]))
    return codes, rows


def test_the_routes_table_lists_exactly_what_it_answers():
    codes, rows = _table_codes()
    assert codes == REACHABLE, (
        f"missing from the table: {sorted(REACHABLE - codes)}; "
        f"listed but never answered: {sorted(codes - REACHABLE)}")
    video_row = next(r for r in rows if 'media_not_a_video' in r)
    voice_row = next(r for r in rows if 'media_not_audio' in r)
    assert '`empty_file`' in video_row and '`empty_file`' in voice_row


def test_the_contract_paragraph_lists_what_the_route_answers():
    text = (ROOT / "API_CONTRACT.md").read_text()
    start = text.index("**Photos, voice notes and videos**")
    paragraph = text[start:text.index("\n\n", start)]
    named = set(re.findall(r'`([a-z_]+)`', paragraph))
    assert REACHABLE <= named, f"not in API_CONTRACT.md: {sorted(REACHABLE - named)}"
    assert not (UNREACHABLE & named), sorted(UNREACHABLE & named)


def test_the_codes_the_table_was_missing_are_really_answered(api, pair, conversation,
                                                             make_user):
    _trainer, client = pair
    as_json = api(client).post(MEDIA, {'conversation': conversation.pk, 'kind': 'image'},
                               content_type="application/json")
    assert as_json.status_code == 415 and as_json.json()['code'] == 'unsupported_media_type'

    no_boundary = api(client).post(MEDIA, data=b"--x\r\nnot a form\r\n--x--",
                                   content_type="multipart/form-data")
    assert no_boundary.status_code == 400, no_boundary.content
    assert no_boundary.json()['code'] == 'parse_error'

    chunked = api(client).post(MEDIA, {'conversation': conversation.pk},
                               HTTP_TRANSFER_ENCODING='chunked')
    assert chunked.status_code == 411 and chunked.json()['code'] == 'length_required'

    nobody = api().post(MEDIA, {'conversation': conversation.pk, 'kind': 'voice'})
    assert nobody.status_code == 401 and nobody.json()['code'] == 'not_authenticated'
    stranger = api().post(MEDIA, {'conversation': conversation.pk, 'kind': 'voice'},
                          HTTP_AUTHORIZATION="Bearer not.a.token")
    assert stranger.status_code == 401 and stranger.json()['code'] == 'token_not_valid'

    head = jpeg_with_gps(300, 300)[:600]
    broken = SimpleUploadedFile("b.jpg", head + os.urandom(3000), content_type="image/jpeg")
    answer = _post(api, client, conversation, 'image', broken)
    assert answer.status_code == 400 and answer.json()['code'] == 'corrupt_image'

    tall = _post(api, client, conversation, 'image', photo(100, 1200))
    assert tall.status_code == 400 and tall.json()['code'] == 'invalid_aspect_ratio'

    named_exe = _post(api, client, conversation, 'image', photo(name="me.exe"))
    assert named_exe.status_code == 400
    assert named_exe.json()['code'] == 'dangerous_extension'

    for kind, name in (('video', 'clip.mp4'), ('voice', 'note.m4a'), ('image', 'a.jpg')):
        empty = _post(api, client, conversation, kind, SimpleUploadedFile(name, b""))
        assert empty.status_code == 400, (kind, empty.content)
        assert empty.json()['code'] == 'empty_file', (kind, empty.json())

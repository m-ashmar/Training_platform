"""A photo, a voice note and a video in the trainer-client chat (BACKEND_BUGS B-091).

Each test names a rule the route promises (`chat/views.py MediaMessageView`):

    the bytes decide        each kind through the platform's own guard, never the label
    the photo is re-encoded  no EXIF / GPS reaches the other person
    private media           signed URL, served in answers only the two people receive;
                            a third party, an unsigned path and a forged token get nothing
    the pair rule           no relation, blocked, frozen: refused BEFORE a byte is stored
    a form check            a client's video naming a visible exercise; nothing else may
    idempotent              a retried upload answers the first message, stores one file
    tombstone               a message taken back stops serving its media
    erasure                 the files go with the account
    the push                a caption-less video still says what it is, never a link

Every "video" and "voice note" is bytes assembled here (the media-kit tests' builders):
the guard reads the bytes, so a real file from disk would prove less.
"""
import io
import os
import shutil
import struct
import subprocess
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from PIL import Image

pytestmark = pytest.mark.django_db

CHAT = '/api/v1/chat'
MEDIA = f'{CHAT}/media/'

needs_ffmpeg = pytest.mark.skipif(
    shutil.which('ffmpeg') is None or shutil.which('ffprobe') is None,
    reason="ffmpeg is not installed on this machine: the real-recording tests need it "
           "(the length readers and the worker are tested on hand-built bytes below)")


@pytest.fixture(autouse=True)
def media_root(settings, tmp_path):
    """Uploads land in a temporary directory, signed exactly as in production."""
    settings.MEDIA_ROOT = str(tmp_path)
    settings.MEDIA_URL_SIGNING = True
    return tmp_path


@pytest.fixture
def pair(make_user):
    from users.models import TrainerClientRelation

    trainer = make_user("cm_trainer", user_type="trainer")
    client = make_user("cm_client", user_type="client")
    TrainerClientRelation.objects.create(trainer=trainer, client=client,
                                         status="approved", initiated_by="client")
    return trainer, client


@pytest.fixture
def conversation(pair):
    from chat.services import open_conversation

    return open_conversation(*pair)


# ------------------------------------------------------------------- the bytes
def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload) + 8) + kind + payload


def _mvhd(seconds: float, timescale: int = 1000) -> bytes:
    body = (b"\x00\x00\x00\x00" + struct.pack(">I", 0) + struct.pack(">I", 0)
            + struct.pack(">I", timescale) + struct.pack(">I", int(seconds * timescale))
            + b"\x00\x01\x00\x00" + b"\x01\x00" + b"\x00" * 10 + b"\x00" * 36
            + b"\x00" * 24 + struct.pack(">I", 2))
    return _box(b"mvhd", body)


def iso_bytes(seconds=4.0, brand=b"isom") -> bytes:
    """An MP4 / M4A head: `ftyp`, a `moov/mvhd` declaring a duration, some `mdat`."""
    return (_box(b"ftyp", brand + struct.pack(">I", 512) + b"isom" + b"mp42")
            + _box(b"moov", _mvhd(seconds)) + _box(b"mdat", b"\x00" * 2048))


def video(seconds=4.0, name="clip.mp4"):
    return SimpleUploadedFile(name, iso_bytes(seconds), content_type="video/mp4")


def voice(seconds=12.0, name="note.m4a"):
    return SimpleUploadedFile(name, iso_bytes(seconds, brand=b"M4A "),
                              content_type="audio/mp4")


def photo_with_gps(w=900, h=1200):
    """A JPEG that carries EXIF, as every phone photo does."""
    image = Image.new("RGB", (w, h), (120, 80, 40))
    exif = Image.Exif()
    exif[0x010F] = "PhoneMaker"                     # Make
    exif[0x8825] = {1: "N", 2: (33.0, 30.0, 0.0)}   # GPSInfo: Damascus-ish
    buf = io.BytesIO()
    image.save(buf, format="JPEG", exif=exif)
    return SimpleUploadedFile("IMG_0001.jpg", buf.getvalue(), content_type="image/jpeg")


def _post(api, user, conversation, kind, file, **extra):
    data = {'conversation': conversation.pk, 'kind': kind, **extra}
    if file is not None:
        data['file'] = file
    return api(user).post(MEDIA, data)


def _stored_files(root):
    chat_dir = os.path.join(str(root), "chat")
    found = []
    for folder, _dirs, files in os.walk(chat_dir):
        found += [os.path.join(folder, name) for name in files]
    return found


# ------------------------------------------------------------------- each kind
def test_a_photo_is_re_encoded_and_served_to_both_people(api, pair, conversation,
                                                         media_root):
    """The platform image guard re-encodes from decoded pixels: the EXIF and the GPS a
    phone photo carries must never reach the other person's phone."""
    trainer, client = pair
    sent = _post(api, client, conversation, 'image', photo_with_gps(), body="my setup")
    assert sent.status_code == 201, sent.content
    message = sent.json()
    assert message['kind'] == 'image'
    assert message['body'] == "my setup"
    assert message['media']['width'] == 900 and message['media']['height'] == 1200
    assert message['exercise'] is None

    [stored] = _stored_files(media_root)
    assert stored.endswith(".jpg"), "the stored name did not come from the detected format"
    with Image.open(stored) as kept:
        assert not kept.getexif(), "the photo kept its EXIF (and its GPS)"

    # The other person reads it in the thread, and the signed URL loads.
    page = api(trainer).get(f"{CHAT}/conversations/{conversation.pk}/messages/").json()
    [row] = page['results']
    assert row['kind'] == 'image' and row['media']['url']
    assert api().get(row['media']['url']).status_code == 200


def test_a_video_with_an_exercise_is_a_form_check_with_its_duration(api, pair,
                                                                   conversation):
    from routine.models import Exercise

    trainer, client = pair
    deadlift = Exercise.objects.create(name="Deadlift")
    sent = _post(api, client, conversation, 'video', video(18.5),
                 exercise_id=deadlift.pk, body="is my back straight?")
    assert sent.status_code == 201, sent.content
    message = sent.json()
    assert message['kind'] == 'video'
    assert message['exercise'] == {'id': deadlift.pk, 'name': "Deadlift"}
    assert message['media']['duration_seconds'] == 18.5, \
        "the duration the container declares was not read"
    assert message['body'] == "is my back straight?"


def test_a_voice_note_is_identified_by_its_bytes(api, pair, conversation, media_root):
    trainer, _client = pair
    sent = _post(api, trainer, conversation, 'voice',
                 voice(24.0, name="whatever.bin"))
    assert sent.status_code == 201, sent.content
    assert sent.json()['kind'] == 'voice'
    assert sent.json()['media']['duration_seconds'] == 24.0
    [stored] = _stored_files(media_root)
    assert stored.endswith(".m4a"), "the client's file name reached storage"


def test_the_route_answers_on_both_spellings(api, pair, conversation):
    """The size middleware matches the exact path: the unslashed spelling must be a
    route of its own, or a video would be refused at 15 MB before any redirect."""
    from django.conf import settings

    _trainer, client = pair
    assert {'/api/v1/chat/media', '/api/v1/chat/media/'} <= set(settings.LARGE_UPLOAD_PATHS)
    response = api(client).post(f'{CHAT}/media', {
        'conversation': conversation.pk, 'kind': 'video', 'file': video()})
    assert response.status_code == 201, response.content


# ---------------------------------------------------------------- the guard
def test_a_file_that_is_not_what_it_says_is_refused_and_nothing_is_stored(
        api, pair, conversation, media_root):
    from chat.models import Message

    _trainer, client = pair
    html = SimpleUploadedFile("clip.mp4", b"<html><script>alert(1)</script></html>" * 4,
                              content_type="video/mp4")
    refused = _post(api, client, conversation, 'video', html)
    assert refused.status_code == 400
    assert refused.json()['code'] == 'media_not_a_video'

    audio_lie = SimpleUploadedFile("note.m4a", b"MZ\x90\x00" + b"\x00" * 200,
                                   content_type="audio/mp4")
    refused = _post(api, client, conversation, 'voice', audio_lie)
    assert refused.status_code == 400 and refused.json()['code'] == 'media_not_audio'

    not_a_photo = SimpleUploadedFile("x.jpg", b"%PDF-1.4 not an image" * 10,
                                     content_type="image/jpeg")
    refused = _post(api, client, conversation, 'image', not_a_photo)
    assert refused.status_code == 400 and refused.json()['code']

    missing = _post(api, client, conversation, 'video', None)
    assert missing.status_code == 400 and missing.json()['code'] == 'media_file_required'

    assert not Message.objects.exists()
    assert not _stored_files(media_root), "a refused upload left bytes in storage"


def test_the_media_kits_caps_apply(api, pair, conversation, settings, media_root):
    _trainer, client = pair
    settings.MEDIA_VIDEO_MAX_SECONDS = 10
    too_long = _post(api, client, conversation, 'video', video(30.0))
    assert too_long.status_code == 400
    assert too_long.json()['code'] == 'media_video_too_long'
    too_long = _post(api, client, conversation, 'voice', voice(30.0))
    assert too_long.status_code == 400
    assert too_long.json()['code'] == 'media_audio_too_long'

    settings.MEDIA_VIDEO_MAX_SECONDS = 90
    settings.MEDIA_VIDEO_MAX_BYTES = 1024
    too_big = _post(api, client, conversation, 'video', video(4.0))
    assert too_big.status_code == 400
    assert too_big.json()['code'] == 'media_video_too_large'
    assert not _stored_files(media_root)


def test_a_bad_form_names_its_code(api, pair, conversation):
    from routine.models import Exercise

    trainer, client = pair
    squat = Exercise.objects.create(name="Squat")
    on_a_photo = _post(api, client, conversation, 'image', photo_with_gps(),
                       exercise_id=squat.pk)
    assert on_a_photo.status_code == 400
    assert on_a_photo.json()['code'] == 'exercise_only_on_video'

    # Another coach's private exercise reads exactly like one that does not exist.
    other = type(trainer).objects.create_user(email="cm_other@gate.test",
                                              username="cm_other", password="Xx!23456")
    other.user_type = 'trainer'
    other.save()
    private = Exercise.objects.create(name="Secret Move", created_by=other)
    for exercise_id in (private.pk, 99999999):
        refused = _post(api, client, conversation, 'video', video(),
                        exercise_id=exercise_id)
        assert refused.status_code == 400, refused.content
        assert refused.json()['code'] == 'unknown_exercise'

    wrong_kind = _post(api, client, conversation, 'sticker', video())
    assert wrong_kind.status_code == 400
    assert wrong_kind.json()['code'] == 'validation_error'


def test_a_guard_refusal_carries_the_api_envelope(api, pair, conversation, settings,
                                                  media_root):
    """BACKEND_BUGS B-149: the upload guard's 400s were written by hand in the view as
    `{error, code}`, so this one route answered two envelope shapes — every other
    refusal of the API (and this route's own `validation_error`) carries
    `field_errors`. They now reach the API's exception handler, which keeps the
    guard's own `code` and writes the one envelope."""
    from routine.models import Exercise

    _trainer, client = pair
    squat = Exercise.objects.create(name="Squat")
    settings.MEDIA_VIDEO_MAX_SECONDS = 10
    not_audio = SimpleUploadedFile("note.m4a", b"MZ\x90\x00" + b"\x00" * 200,
                                   content_type="audio/mp4")
    cases = [
        ('voice', not_audio, {}, 'media_not_audio'),
        ('video', video(30.0), {}, 'media_video_too_long'),
        ('video', None, {}, 'media_file_required'),
        ('image', photo_with_gps(), {'exercise_id': squat.pk}, 'exercise_only_on_video'),
    ]
    for kind, file, extra, code in cases:
        refused = _post(api, client, conversation, kind, file, **extra)
        assert refused.status_code == 400, (code, refused.content)
        body = refused.json()
        assert body['code'] == code, body
        assert body['detail'] and body['error'] == body['detail'], body
        assert 'field_errors' in body, f"{code}: no field_errors in {body}"
        [entry] = body['field_errors']['non_field_errors']
        assert entry['message'] == body['detail'], body
        assert body['non_field_errors'] == [body['detail']], body
    assert not _stored_files(media_root)


# ---------------------------------------------------------------- privacy
def test_only_the_two_people_in_the_thread_ever_get_the_media(api, make_user, pair,
                                                              conversation):
    """The URL is the credential (an image load carries no JWT), so it is handed out
    only inside answers the two members receive — and the path alone, a forged token
    and another file's token open nothing."""
    trainer, client = pair
    sent = _post(api, client, conversation, 'video', video()).json()
    url = sent['media']['url']
    path, _sep, query = url.partition('?')
    assert query.startswith('s='), "a chat video is served on an unsigned URL"

    outsider_client = make_user("cm_outsider_c", user_type="client")
    outsider_trainer = make_user("cm_outsider_t", user_type="trainer")
    for outsider in (outsider_client, outsider_trainer):
        history = api(outsider).get(f"{CHAT}/conversations/{conversation.pk}/messages/")
        assert history.status_code == 404
        into = _post(api, outsider, conversation, 'video', video())
        assert into.status_code == 404 and into.json()['code'] == 'not_found'
        listed = api(outsider).get(f"{CHAT}/conversations/").json()
        assert str(path) not in str(listed)

    visitor = api()
    assert visitor.get(url).status_code == 200
    assert visitor.get(path).status_code == 404, "the bare path serves a private video"
    assert visitor.get(f"{path}?s=deadbeefdeadbeef").status_code == 404
    other = _post(api, client, conversation, 'image', photo_with_gps()).json()
    other_token = other['media']['url'].partition('?')[2]
    assert visitor.get(f"{path}?{other_token}").status_code == 404

    # The list and the push quote WHAT it is, never where it lives.
    row = api(trainer).get(f"{CHAT}/conversations/").json()['results'][0]
    assert row['last_message']['kind'] == 'image'
    assert 'media' not in row['last_message']
    from notifications.models import Notification
    for note in Notification.objects.filter(event_type='chat_message'):
        assert '/media/' not in str(note.metadata)


def test_a_conversation_that_cannot_exist_is_the_same_404(api, pair):
    """A missing, malformed or out-of-range id answers the stranger's 404, not a 500."""
    _trainer, client = pair
    for conversation_id in ('', 'abc', '99999999', str(2 ** 70)):
        response = api(client).post(MEDIA, {'conversation': conversation_id,
                                            'kind': 'video', 'file': video()})
        assert response.status_code == 404, (conversation_id, response.content)
        assert response.json()['code'] == 'not_found'


def test_a_message_taken_back_stops_serving_its_media(api, pair, conversation):
    trainer, client = pair
    sent = _post(api, client, conversation, 'image', photo_with_gps()).json()
    gone = api(client).delete(f"{CHAT}/messages/{sent['id']}/")
    assert gone.status_code == 200
    assert gone.json()['media'] is None and gone.json()['deleted'] is True
    page = api(trainer).get(f"{CHAT}/conversations/{conversation.pk}/messages/").json()
    assert page['results'][0]['media'] is None


def test_erasing_an_account_deletes_the_files(api, pair, conversation, media_root,
                                              django_capture_on_commit_callbacks):
    """A private thread is joint data and goes with either account; the stored photos,
    voice notes and videos of BOTH sides go with it, not just the rows."""
    from training_platform.privacy.registry import erase_user_data

    trainer, client = pair
    assert _post(api, client, conversation, 'video', video()).status_code == 201
    assert _post(api, trainer, conversation, 'voice', voice()).status_code == 201
    assert len(_stored_files(media_root)) == 2

    with django_capture_on_commit_callbacks(execute=True):
        erase_user_data(client)
    assert not _stored_files(media_root), "erasure left the chat's files in storage"


# ---------------------------------------------------------------- the pair rule
@pytest.fixture
def frozen(pair):
    """A settled month that ended with `frozen_at` — what `refusals_for` reads as
    `coaching_frozen` (the same row `users.coaching` writes)."""
    from users.models import CoachingPeriod, TrainerClientRelation

    trainer, client = pair
    relation = TrainerClientRelation.objects.get(trainer=trainer, client=client)
    now = timezone.now()
    CoachingPeriod.objects.create(
        relation=relation, trainer=trainer, client=client, number=1,
        price=Decimal("100.00"), commission_rate=Decimal("0.2000"), status='settled',
        hold_reference=str(uuid.uuid4()),
        starts_at=now - timedelta(days=31), ends_at=now - timedelta(days=1),
        frozen_at=now - timedelta(days=1))
    return trainer, client


def test_a_frozen_coaching_refuses_media_before_storing_it(api, frozen, conversation,
                                                           media_root):
    trainer, client = frozen
    for who in (client, trainer):
        refused = _post(api, who, conversation, 'video', video())
        assert refused.status_code == 403, refused.content
        assert refused.json()['code'] == 'coaching_frozen'
        assert refused.json()['trainer_id'] == trainer.pk
    assert not _stored_files(media_root)


def test_a_block_refuses_media_before_storing_it(api, pair, conversation, media_root):
    from moderation.models import Block

    trainer, client = pair
    Block.objects.create(blocker=trainer, blocked=client)
    refused = _post(api, client, conversation, 'image', photo_with_gps())
    assert refused.status_code == 403 and refused.json()['code'] == 'blocked'
    assert not _stored_files(media_root)


def test_a_retried_upload_is_one_message_and_one_file(api, pair, conversation,
                                                      media_root):
    from chat.models import Message

    _trainer, client = pair
    key = str(uuid.uuid4())
    first = _post(api, client, conversation, 'video', video(), client_uuid=key)
    assert first.status_code == 201, first.content
    retry = _post(api, client, conversation, 'video', video(), client_uuid=key)
    assert retry.status_code == 200
    assert retry.json()['id'] == first.json()['id']
    assert Message.objects.count() == 1
    assert len(_stored_files(media_root)) == 1, "the retry stored a second copy"


# ---------------------------------------------------------------- the push
def test_a_video_without_a_caption_says_what_it_is_in_the_recipients_language(
        api, pair, conversation):
    from notifications.models import Notification

    trainer, client = pair
    trainer.preferred_language = 'ar'
    trainer.save(update_fields=['preferred_language'])
    assert _post(api, client, conversation, 'video', video()).status_code == 201
    note = Notification.objects.get(recipient=trainer, event_type='chat_message')
    assert note.metadata['context']['preview'] == "فيديو"
    assert note.metadata['data']['message_id']


# ------------------------------------------------- the length of a recording (B-177)
# BACKEND_BUGS B-177: an Ogg or WebM voice note (or WebM video) stored
# `duration_seconds = None` — the declared-duration reader read MP4 only — so the card's
# clock read 0:00, and the 90 s cap was never applied to those containers (about 50
# minutes of Opus fit under the 10 MB cap). The request now reads an Ogg's last page and
# a WebM's Info/Duration, bounded and without decoding, and a worker measures what
# declares nothing (a WebM a browser recorded live).
def _ogg_page(payload: bytes, *, granule: int, sequence: int, flags: int = 0,
              serial: int = 7) -> bytes:
    """One Ogg page around `payload` (the CRC left zero: the reader does not check it)."""
    lacing, rest = [], len(payload)
    while rest >= 255:
        lacing.append(255)
        rest -= 255
    lacing.append(rest)
    return (b"OggS" + bytes([0, flags]) + struct.pack("<Q", granule)
            + struct.pack("<I", serial) + struct.pack("<I", sequence) + b"\x00" * 4
            + bytes([len(lacing)]) + bytes(lacing) + payload)


def ogg_opus_bytes(seconds: float, pre_skip: int = 312) -> bytes:
    """An Ogg Opus stream as a recorder writes it: the OpusHead page, the tags page,
    audio pages, and a last page whose granule says `seconds` (plus the pre-skip)."""
    head = (b"OpusHead" + bytes([1, 1]) + struct.pack("<H", pre_skip)
            + struct.pack("<I", 48000) + b"\x00\x00\x00")
    return b"".join([
        _ogg_page(head, granule=0, sequence=0, flags=0x02),
        _ogg_page(b"OpusTags" + b"\x00" * 16, granule=0, sequence=1),
        _ogg_page(b"\x5a" * 3000, granule=48000 + pre_skip, sequence=2),
        _ogg_page(b"\x5a" * 300, granule=int(seconds * 48000) + pre_skip, sequence=3,
                  flags=0x04),
    ])


def _ebml(element_id: bytes, payload: bytes, *, unknown_size=False) -> bytes:
    size = (b"\x01" + b"\xff" * 7 if unknown_size
            else ((1 << 56) | len(payload)).to_bytes(8, "big"))
    return element_id + size + payload


def webm_bytes(*, ticks=None, scale=1_000_000, cluster_first=False) -> bytes:
    """A WebM head: the EBML header, a Segment of unknown size (as a live recorder
    writes it) holding a Void, Info — with `Duration` = `ticks` of `scale` ns, or none
    — and a Cluster."""
    children = _ebml(b"\x2a\xd7\xb1", scale.to_bytes(4, "big"))
    if ticks is not None:
        children = _ebml(b"\x44\x89", struct.pack(">d", ticks)) + children
    info = _ebml(b"\x15\x49\xa9\x66", children)
    cluster = _ebml(b"\x1f\x43\xb6\x75", b"\x00" * 512)
    body = _ebml(b"\xec", b"\x00" * 40) + (cluster + info if cluster_first
                                            else info + cluster)
    return (_ebml(b"\x1a\x45\xdf\xa3", _ebml(b"\x42\x82", b"webm"))
            + _ebml(b"\x18\x53\x80\x67", body, unknown_size=True))


def test_the_declared_length_of_an_ogg_and_a_webm_is_read_without_decoding(monkeypatch):
    from training_platform import file_security
    from training_platform.file_security import probe_declared_duration

    def read(data, container):
        return probe_declared_duration(io.BytesIO(data), container)

    assert read(ogg_opus_bytes(7.0), 'ogg') == 7.0
    assert read(ogg_opus_bytes(95.5, pre_skip=0), 'ogg') == 95.5
    assert read(webm_bytes(ticks=12345.0), 'matroska') == 12.345
    assert read(webm_bytes(ticks=4000.0, scale=500_000), 'matroska') == 2.0

    # What declares nothing is still None — never a guess.
    assert read(webm_bytes(), 'matroska') is None, "a live WebM has no Duration"
    assert read(webm_bytes(ticks=9000.0, cluster_first=True), 'matroska') is None
    # An Ogg that does not END on a whole page (cut short, or with bytes after it).
    whole = ogg_opus_bytes(7.0)
    assert read(whole[:-5], 'ogg') is None
    assert read(whole + b"\x00" * 64, 'ogg') is None
    # Four bytes "OggS" inside the last page's audio are not a page.
    fake = whole[:-200] + b"OggS" + b"\x5a" * 196
    assert read(fake, 'ogg') == 7.0
    # Bounded: a tail of nothing but false starts gives up, it does not walk them all
    # (the head's page, then at most OGG_MAX_CANDIDATES in the tail).
    walked = []
    real_page = file_security._ogg_page
    monkeypatch.setattr(file_security, '_ogg_page',
                        lambda *a: walked.append(1) or real_page(*a))
    assert read(whole[:200] + b"OggS" * 3200, 'ogg') is None
    assert len(walked) <= file_security.OGG_MAX_CANDIDATES + 1, len(walked)
    monkeypatch.undo()
    # A codec whose clock the reader does not know is left to the worker.
    flac = whole.replace(b"OpusHead", b"\x7fFLAC\x01\x00\x00", 1)
    assert read(flac, 'ogg') is None


def test_a_length_that_rounds_to_nothing_is_none_and_measured_later(
        api, pair, conversation, monkeypatch, django_capture_on_commit_callbacks):
    """The adversarial review of B-177 (2026-11-24), found by fuzzing the readers: a
    declared length under half a millisecond rounded to 0.0 and was returned AS the
    length — an Ogg whose last granule is its pre-skip plus one sample, a WebM whose
    Duration is a fraction of a tick, an MP4 of one tick in ten thousand. It was stored,
    and the worker measures only a recording with NO length (`send_media_message`
    queues it for `duration_seconds is None`, `fill_media_duration` skips any other),
    so the note read 0:00 for good. Each is None now, and the send queues the worker."""
    from training_platform.file_security import probe_declared_duration

    def read(data, container):
        return probe_declared_duration(io.BytesIO(data), container)

    tiny_ogg = ogg_opus_bytes(1 / 48000)                  # granule = pre-skip + 1
    one_tick = (_box(b"ftyp", b"M4A " + struct.pack(">I", 512) + b"isom")
                + _box(b"moov", _mvhd(0.0001, timescale=10000))
                + _box(b"mdat", b"\x00" * 64))
    assert read(tiny_ogg, 'ogg') is None
    assert read(webm_bytes(ticks=0.0001), 'matroska') is None
    assert read(one_tick, 'iso') is None
    # A real length still reads, however short (two of Opus's 2.5 ms frames).
    assert read(ogg_opus_bytes(0.005), 'ogg') == 0.005

    trainer, _client = pair
    queued = []
    monkeypatch.setattr("chat.media.queue_duration", queued.append)
    with django_capture_on_commit_callbacks(execute=True):
        sent = _post(api, trainer, conversation, 'voice',
                     SimpleUploadedFile("tiny.ogg", tiny_ogg, content_type="audio/ogg"))
    assert sent.status_code == 201, sent.content
    assert sent.json()['media']['duration_seconds'] is None
    assert queued == [sent.json()['id']], "the worker was not asked to measure it"


def test_an_ogg_or_webm_voice_note_is_capped_and_timed_on_the_request(
        api, pair, conversation, settings, media_root):
    """The route, on hand-built bytes (no ffmpeg needed): the length is on the answer,
    and the 90 s cap — the same refusal code as an M4A's — now holds for them too."""
    from chat.models import Message

    trainer, _client = pair
    for name, data, seconds in (("note.ogg", ogg_opus_bytes(7.0), 7.0),
                                ("note.webm", webm_bytes(ticks=12345.0), 12.345)):
        sent = _post(api, trainer, conversation, 'voice',
                     SimpleUploadedFile(name, data, content_type="audio/ogg"))
        assert sent.status_code == 201, sent.content
        assert sent.json()['media']['duration_seconds'] == seconds

    settings.MEDIA_VIDEO_MAX_SECONDS = 5
    before = Message.objects.count()
    for data in (ogg_opus_bytes(7.0), webm_bytes(ticks=12345.0)):
        refused = _post(api, trainer, conversation, 'voice',
                        SimpleUploadedFile("long.ogg", data, content_type="audio/ogg"))
        assert refused.status_code == 400, refused.content
        assert refused.json()['code'] == 'media_audio_too_long'
    long_video = _post(api, trainer, conversation, 'video',
                       SimpleUploadedFile("clip.webm", webm_bytes(ticks=12345.0),
                                          content_type="video/webm"))
    assert long_video.status_code == 400
    assert long_video.json()['code'] == 'media_video_too_long'
    assert Message.objects.count() == before


def _recording(tmp_path, name, *args):
    """A real recording the local ffmpeg makes from a generated tone; skipped where
    this ffmpeg lacks the encoder."""
    out = tmp_path / name
    made = subprocess.run([shutil.which('ffmpeg'), '-v', 'error', '-y', '-f', 'lavfi',
                           '-i', 'sine=frequency=440:duration=3', *args, str(out)],
                          capture_output=True, timeout=60)
    if made.returncode != 0:
        pytest.skip(f"this ffmpeg cannot write {name}: {made.stderr[-200:]!r}")
    return out.read_bytes()


@needs_ffmpeg
def test_an_ogg_voice_note_carries_its_length(api, pair, conversation, tmp_path,
                                              monkeypatch,
                                              django_capture_on_commit_callbacks):
    """The proving test of B-177: a real three-second Opus/Ogg voice note (what an
    Android recorder writes) — and a Vorbis one, and a WebM written to a file — is
    answered with its length, read on the request: no worker is queued for a
    container that declares one."""
    from chat import media

    queued = []
    # `raising=False`: so that, on the code before B-177 (which had no worker), this
    # fails on the missing length and not on the stand-in.
    monkeypatch.setattr(media, 'queue_duration', queued.append, raising=False)
    trainer, client = pair
    for name, args in (("opus.ogg", ('-c:a', 'libopus')),
                       ("vorbis.ogg", ('-c:a', 'libvorbis')),
                       ("seekable.webm", ('-c:a', 'libopus'))):
        note = SimpleUploadedFile(name, _recording(tmp_path, name, *args),
                                  content_type="audio/ogg")
        with django_capture_on_commit_callbacks(execute=True):
            sent = _post(api, client, conversation, 'voice', note)
        assert sent.status_code == 201, sent.content
        seconds = sent.json()['media']['duration_seconds']
        assert seconds is not None and abs(seconds - 3.0) < 0.1, (name, seconds)
    assert queued == [], "a declared length was sent to the worker"
    newest = api(trainer).get(
        f"{CHAT}/conversations/{conversation.pk}/messages/").json()['results'][0]
    assert abs(newest['media']['duration_seconds'] - 3.0) < 0.1


@needs_ffmpeg
def test_a_webm_recorded_live_is_measured_by_a_worker(
        api, pair, conversation, tmp_path, monkeypatch,
        django_capture_on_commit_callbacks):
    """A browser's MediaRecorder writes a WebM with no Duration (ffmpeg writing to a
    pipe makes the same file, and its `format=duration` is N/A). The send answers with
    null; once the row commits a worker measures the recording with ffprobe — the end
    of its last packet — stores it, and the thread hears `message.updated`."""
    from chat import events
    from chat.models import Message

    trainer, client = pair
    out = tmp_path / "live.webm"
    with open(out, "wb") as sink:
        made = subprocess.run([shutil.which('ffmpeg'), '-v', 'error', '-f', 'lavfi', '-i',
                               'sine=frequency=440:duration=3', '-c:a', 'libopus',
                               '-f', 'webm', 'pipe:1'],
                              stdout=sink, stderr=subprocess.PIPE, timeout=60)
    if made.returncode != 0:
        pytest.skip(f"this ffmpeg cannot write a live WebM: {made.stderr[-200:]!r}")
    frames = []
    monkeypatch.setattr(events, 'broadcast',
                        lambda conversation_id, kind, payload:
                        frames.append((kind, payload)))

    with django_capture_on_commit_callbacks(execute=True):
        sent = _post(api, client, conversation, 'voice',
                     SimpleUploadedFile("live.webm", out.read_bytes(),
                                        content_type="audio/webm"))
        assert sent.status_code == 201, sent.content
        assert sent.json()['media']['duration_seconds'] is None
    stored = Message.objects.get(pk=sent.json()['id']).duration_seconds
    assert stored is not None and abs(stored - 3.0) < 0.1, stored
    [(_kind, payload)] = [f for f in frames if f[0] == 'message.updated']
    assert payload['message']['id'] == sent.json()['id']
    assert payload['message']['media']['duration_seconds'] == stored
    [row] = api(trainer).get(
        f"{CHAT}/conversations/{conversation.pk}/messages/").json()['results']
    assert row['media']['duration_seconds'] == stored


def _stored_recording(conversation, sender, *, duration=None, kind='voice'):
    from django.core.files.base import ContentFile

    from chat.models import Message

    row = Message(conversation=conversation, sender=sender, kind=kind,
                  duration_seconds=duration)
    row.attachment.save(f"{kind}.webm", ContentFile(webm_bytes()), save=False)
    row.save()
    return row


def test_the_length_worker_without_ffprobe_downloads_nothing(pair, conversation,
                                                             monkeypatch, caplog):
    """The media kit's rule, kept: with no ffprobe on the worker nothing is written,
    nothing is downloaded, and the worker says so once — not once per message."""
    import logging

    from chat.tasks import fill_media_duration
    from mediakit import tasks as video_tools

    _trainer, client = pair
    rows = [_stored_recording(conversation, client) for _ in range(2)]
    monkeypatch.setattr(video_tools.shutil, "which", lambda name: None)
    monkeypatch.setattr(video_tools, "_MISSING_TOOLS", set())
    downloads = []
    monkeypatch.setattr(video_tools, "local_copy_of",
                        lambda f: downloads.append(f.name) or ("/nonexistent", False))
    with caplog.at_level(logging.WARNING, logger="mediakit.tasks"):
        results = [fill_media_duration(row.pk) for row in rows]
    assert [r['state'] for r in results] == ['unmeasured', 'unmeasured']
    assert downloads == [], "a recording was downloaded with no ffprobe to read it"
    said = [r for r in caplog.records if "ffprobe is not installed" in r.getMessage()]
    assert len(said) == 1, [r.getMessage() for r in caplog.records]
    for row in rows:
        row.refresh_from_db()
        assert row.duration_seconds is None


def test_the_length_worker_writes_only_onto_the_row_it_measured(pair, conversation,
                                                                 monkeypatch):
    """At-least-once delivery and a message taken back mid-run: a known length is never
    overwritten, a photo or a missing row is skipped, and a recording its sender took
    back while it was measured gets no length."""
    from chat import tasks
    from chat.services import tombstone

    _trainer, client = pair
    known = _stored_recording(conversation, client, duration=12.0)
    assert tasks.fill_media_duration(known.pk)['state'] == 'skipped'
    assert tasks.fill_media_duration(99999999)['state'] == 'missing'
    photo = _stored_recording(conversation, client, kind='image')
    assert tasks.fill_media_duration(photo.pk)['state'] == 'skipped'

    taken_back = _stored_recording(conversation, client)

    def measure_while_taken_back(_source):
        tombstone(type(taken_back).objects.get(pk=taken_back.pk), client)
        return 3.0

    monkeypatch.setattr(tasks, "_measured_seconds", measure_while_taken_back)
    assert tasks.fill_media_duration(taken_back.pk)['state'] == 'superseded'
    taken_back.refresh_from_db()
    assert taken_back.duration_seconds is None
    assert tasks.fill_media_duration(taken_back.pk)['state'] == 'skipped'

    fresh = _stored_recording(conversation, client)
    monkeypatch.setattr(tasks, "_measured_seconds", lambda _source: 3.25)
    assert tasks.fill_media_duration(fresh.pk) == {
        "message": fresh.pk, "duration": 3.25, "state": "ready"}
    fresh.refresh_from_db()
    assert fresh.duration_seconds == 3.25
    known.refresh_from_db()
    assert known.duration_seconds == 12.0

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
import struct
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

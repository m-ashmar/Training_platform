"""Program cards and video poster frames in the trainer-client chat (BACKEND_DELTA item 8
remainder, plan step 8 "Left", 2026-09-27).

Each test names a rule the code promises:

  PROGRAM CARDS (`chat/services.py _send_program`, `chat/serializers.py program_card`)
    the card            a trainer's card names a routine they wrote that THIS client is
                        on, and carries what the design draws: «حدّث برنامجك · برنامج القوة
                        · يوم الدفع» — the name, the day and its name, what changed
    a template          names the client's own routine written from it
    every refusal       names its own code, and writes nothing
    one read rule       the client sees the card only while they may READ the routine
                        (`routine.permissions.readable_routines`): frozen still reads,
                        taken off the routine does not
    one send path       REST and the socket, one replay, one fan-out, one push
    bounded             a page of history decides every card on it at once

  POSTER FRAMES (`chat/tasks.py`, `mediakit/tasks.py`)
    off the request     the send never decodes; a worker cuts the frame after commit
    private             stored beside the video, served signed, gone with it
    no ffmpeg           no poster, no download, said once per worker
    the image           the Dockerfile installs ffmpeg

The ffmpeg-dependent tests build a real two-second video with the local ffmpeg and are
SKIPPED where it is absent (`needs_ffmpeg`); the task wiring and the payload are tested
either way with the frame cutter stood in for.
"""
import io
import json
import logging
import os
import re
import shutil
import struct
import subprocess
import uuid
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from asgiref.sync import async_to_sync
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test.utils import CaptureQueriesContext, override_settings
from django.utils import timezone
from PIL import Image

pytestmark = pytest.mark.django_db

ROOT = Path(__file__).resolve().parent.parent
CHAT = '/api/v1/chat'
MEDIA = f'{CHAT}/media/'
JSON = 'application/json'

needs_ffmpeg = pytest.mark.skipif(
    shutil.which('ffmpeg') is None,
    reason="ffmpeg is not installed on this machine: the real-frame tests need it "
           "(the task wiring and the payload are tested without it below)")


# --------------------------------------------------------------------------- setup
@pytest.fixture(autouse=True)
def media_root(settings, tmp_path):
    """Uploads land in a temporary directory, signed exactly as in production."""
    settings.MEDIA_ROOT = str(tmp_path)
    settings.MEDIA_URL_SIGNING = True
    return tmp_path


@pytest.fixture
def pair(make_user):
    from users.models import TrainerClientRelation

    trainer = make_user("ccp_trainer", user_type="trainer")
    client = make_user("ccp_client", user_type="client", preferred_language="ar")
    TrainerClientRelation.objects.create(trainer=trainer, client=client,
                                         status="approved", initiated_by="client")
    return trainer, client


@pytest.fixture
def conversation(pair):
    from chat.services import open_conversation

    return open_conversation(*pair)


def _exercise(owner, name):
    from routine.models import Exercise

    return Exercise.objects.create(name=name, created_by=owner)


def _program(trainer, client, *, name="Strength program", days=4, rows=((2, 3),),
             template=None):
    """A routine `trainer` wrote and `client` is on — the client's private copy — with
    `n` exercise rows on each `(day, n)` of `rows`, and an Arabic name."""
    from routine.models import Routine, RoutineExercise

    routine = Routine.objects.create(
        name=name, days=days, created_by=trainer, source_template=template,
        translations={'ar': {'name': 'برنامج القوة'}},
        day_names={"2": "دفع"})
    routine.assigned_to.add(client)
    for day, count in rows:
        for order in range(1, count + 1):
            RoutineExercise.objects.create(
                routine=routine, exercise=_exercise(trainer, f"ccp {name} {day}.{order}"),
                day=day, order=order)
    return routine


def _card(api, user, conversation, **fields):
    return api(user).post(f"{CHAT}/conversations/{conversation.pk}/messages/",
                          {'kind': 'program', **fields}, content_type=JSON)


def _history(api, user, conversation, **headers):
    response = api(user).get(f"{CHAT}/conversations/{conversation.pk}/messages/",
                             **headers)
    assert response.status_code == 200, response.content
    return response.json()['results']


# =========================================================================== CARDS
def test_a_trainer_sends_a_card_the_client_opens_as_the_routine_it_names(
        api, pair, conversation):
    """The design's card: «حدّث برنامجك · برنامج القوة · يوم الدفع». It names the routine,
    the day and that day's name, and the client opens it as that routine."""
    trainer, client = pair
    routine = _program(trainer, client)

    sent = _card(api, trainer, conversation, routine_id=routine.pk, day=2,
                 body="خففت وزن ضغط الأكتاف")
    assert sent.status_code == 201, sent.content
    message = sent.json()
    assert message['kind'] == 'program'
    assert message['body'] == "خففت وزن ضغط الأكتاف"
    assert message['media'] is None and message['exercise'] is None
    assert message['program'] == {
        'routine_id': routine.pk, 'template_id': None, 'name': "Strength program",
        'day': 2, 'day_name': "دفع", 'exercises': 3, 'changes': None}

    # The client reads it in their language, and opens it as the routine it names.
    [row] = _history(api, client, conversation, HTTP_ACCEPT_LANGUAGE='ar')
    assert row['kind'] == 'program'
    assert row['program']['name'] == "برنامج القوة"
    assert row['program']['routine_id'] == routine.pk
    opened = api(client).get(f"/api/routine/routines/{routine.pk}/")
    assert opened.status_code == 200, opened.content

    # A card without a day opens the whole routine.
    whole = _card(api, trainer, conversation, routine_id=routine.pk).json()
    assert (whole['program']['day'], whole['program']['day_name']) == (None, None)
    assert whole['body'] == ""


def test_a_template_card_names_the_clients_own_copy_of_it(api, pair, conversation):
    """A trainer may name the program by the template they wrote it from; the card
    still opens the client's PRIVATE routine (routine/assignment.py), never the
    template, and the newest active copy when there are two."""
    from routine.models import Routine, RoutineTemplate

    trainer, client = pair
    # A code: the model takes only the closed goal list (wave-1 review R-5, 2026-10-07).
    template = RoutineTemplate.objects.create(name="ccp tpl", goal="strength", days=3,
                                              created_by=trainer)
    old = _program(trainer, client, name="old copy", template=template)
    Routine.objects.filter(pk=old.pk).update(is_active=False)
    copy = _program(trainer, client, name="current copy", template=template)

    sent = _card(api, trainer, conversation, template_id=template.pk, day=2)
    assert sent.status_code == 201, sent.content
    assert sent.json()['program']['routine_id'] == copy.pk
    assert sent.json()['program']['template_id'] == template.pk


def test_every_refusal_names_its_own_code_and_writes_nothing(api, make_user, pair,
                                                             conversation):
    from chat.models import Message
    from routine.models import Routine, RoutineTemplate

    trainer, client = pair
    mine = _program(trainer, client)
    not_on_it = Routine.objects.create(name="ccp another client's", days=3,
                                       created_by=trainer)
    stranger = make_user("ccp_other_trainer", user_type="trainer")
    theirs = Routine.objects.create(name="ccp theirs", days=3, created_by=stranger)
    theirs.assigned_to.add(client)
    # Codes: the model takes only the closed goal list (wave-1 review R-5, 2026-10-07).
    uncopied = RoutineTemplate.objects.create(name="ccp uncopied", goal="strength", days=3,
                                              created_by=trainer)
    their_template = RoutineTemplate.objects.create(name="ccp their tpl", goal="strength",
                                                    days=3, created_by=stranger)

    cases = [
        (client, {'routine_id': mine.pk}, 403, 'program_trainer_only'),
        (trainer, {}, 400, 'program_target_required'),
        (trainer, {'routine_id': mine.pk, 'template_id': uncopied.pk}, 400,
         'program_target_required'),
        (trainer, {'routine_id': 'x'}, 400, 'validation_error'),
        (trainer, {'routine_id': 99999999}, 400, 'unknown_program'),
        # Another trainer's routine, even one this client is on, answers exactly like an
        # id that does not exist.
        (trainer, {'routine_id': theirs.pk}, 400, 'unknown_program'),
        (trainer, {'template_id': their_template.pk}, 400, 'unknown_program'),
        (trainer, {'routine_id': not_on_it.pk}, 400, 'program_not_for_this_client'),
        (trainer, {'template_id': uncopied.pk}, 400, 'program_not_for_this_client'),
        (trainer, {'routine_id': mine.pk, 'day': 5}, 400, 'day_out_of_range'),
        (trainer, {'routine_id': mine.pk, 'day': 0}, 400, 'validation_error'),
    ]
    for sender, fields, status, code in cases:
        refused = _card(api, sender, conversation, **fields)
        assert (refused.status_code, refused.json()['code']) == (status, code), \
            (fields, refused.content)
    # A photo on the text route is its own mistake, with its own code.
    wrong_kind = api(trainer).post(f"{CHAT}/conversations/{conversation.pk}/messages/",
                                   {'kind': 'image', 'body': 'x'}, content_type=JSON)
    assert (wrong_kind.status_code, wrong_kind.json()['code']) == (400, 'invalid_kind')
    assert not Message.objects.exists(), "a refused card was written"


def test_the_coaching_rules_decide_who_sends_and_reading_outlives_them(
        api, pair, conversation):
    """The owner's freeze (2026-09-19): a frozen coaching keeps the coach's routines
    READABLE and refuses new work. So the trainer cannot send a card into a frozen
    coaching, and a card sent while it was paid still opens for the client — the rule
    the routine route itself follows."""
    from users.models import CoachingPeriod, TrainerClientRelation

    trainer, client = pair
    routine = _program(trainer, client)
    assert _card(api, trainer, conversation, routine_id=routine.pk,
                 day=2).status_code == 201

    relation = TrainerClientRelation.objects.get(trainer=trainer, client=client)
    now = timezone.now()
    CoachingPeriod.objects.create(
        relation=relation, trainer=trainer, client=client, number=1,
        price=Decimal("100.00"), commission_rate=Decimal("0.2000"), status='settled',
        hold_reference=str(uuid.uuid4()),
        starts_at=now - timedelta(days=31), ends_at=now - timedelta(days=1),
        frozen_at=now - timedelta(days=1))

    refused = _card(api, trainer, conversation, routine_id=routine.pk)
    assert refused.status_code == 403, refused.content
    assert refused.json()['code'] == 'coaching_frozen'
    assert refused.json()['trainer_id'] == trainer.pk

    [row] = _history(api, client, conversation)
    assert row['program'] is not None, "a frozen client lost a card they may still read"
    assert row['program']['routine_id'] == routine.pk
    assert api(client).get(f"/api/routine/routines/{routine.pk}/").status_code == 200


def test_a_card_is_served_only_while_its_reader_may_read_the_routine(api, pair,
                                                                    conversation):
    """Taken off the routine, the client no longer reads it (`GET /api/routine/
    routines/{id}/` answers 404), and the card in their thread says so: `program: null`,
    the row still in its place. The trainer who wrote it still sees it. A deleted
    routine leaves the card for both, opening nothing."""
    trainer, client = pair
    routine = _program(trainer, client)
    sent = _card(api, trainer, conversation, routine_id=routine.pk, day=2).json()

    routine.assigned_to.remove(client)
    assert api(client).get(f"/api/routine/routines/{routine.pk}/").status_code == 404
    [row] = _history(api, client, conversation)
    assert row['id'] == sent['id'] and row['kind'] == 'program'
    assert row['program'] is None, "the card still opened a routine the client lost"
    [row] = _history(api, trainer, conversation)
    assert row['program']['routine_id'] == routine.pk

    from routine.models import Routine, RoutineProgress

    # Progress rows PROTECT a routine; a routine nobody trained on can be deleted.
    RoutineProgress.objects.filter(routine=routine).delete()
    Routine.objects.filter(pk=routine.pk).delete()
    for reader in pair:
        [row] = _history(api, reader, conversation)
        assert row['kind'] == 'program' and row['program'] is None

    # A card its sender took back keeps its place and loses its card.
    fresh = _program(trainer, client, name="ccp second")
    card = _card(api, trainer, conversation, routine_id=fresh.pk).json()
    gone = api(trainer).delete(f"{CHAT}/messages/{card['id']}/")
    assert gone.status_code == 200 and gone.json()['program'] is None


def test_what_changed_since_the_previous_card(api, pair, conversation):
    """«حدّث برنامجك»: the card says what changed on that day since the last card for the
    same routine — rows added, edited and removed — written once, at send."""
    from routine.models import RoutineExercise

    trainer, client = pair
    routine = _program(trainer, client, rows=((2, 3), (3, 1)))
    first = _card(api, trainer, conversation, routine_id=routine.pk, day=2).json()
    assert first['program']['exercises'] == 3 and first['program']['changes'] is None

    later = timezone.now() + timedelta(seconds=1)
    rows = list(RoutineExercise.objects.filter(routine=routine, day=2).order_by('order'))
    rows[0].delete()                                     # removed
    rows[1].sets = 5
    rows[1].save()                                       # edited
    RoutineExercise.objects.create(routine=routine, day=2, order=9,
                                   exercise=_exercise(trainer, "ccp added"))  # added
    # Stamps a second on, so "after the first card" does not hang on clock resolution.
    RoutineExercise.objects.filter(pk=rows[1].pk).update(updated_at=later)
    RoutineExercise.objects.filter(order=9, routine=routine).update(
        created_at=later, updated_at=later)

    second = _card(api, trainer, conversation, routine_id=routine.pk, day=2).json()
    changes = second['program']['changes']
    assert second['program']['exercises'] == 3
    assert (changes['added'], changes['edited'], changes['removed']) == (1, 1, 1), changes
    assert changes['since'] is not None

    # A card about another day cannot say what was removed from this one.
    other_day = _card(api, trainer, conversation, routine_id=routine.pk, day=3).json()
    assert other_day['program']['changes']['removed'] is None


def test_a_retried_card_is_one_message(api, pair, conversation):
    from chat.models import Message

    trainer, client = pair
    routine = _program(trainer, client)
    key = str(uuid.uuid4())
    first = _card(api, trainer, conversation, routine_id=routine.pk, client_uuid=key)
    retry = _card(api, trainer, conversation, routine_id=routine.pk, client_uuid=key)
    assert (first.status_code, retry.status_code) == (201, 200)
    assert retry.json()['id'] == first.json()['id']
    assert Message.objects.filter(kind='program').count() == 1


def test_the_card_reaches_the_list_and_the_push(api, pair, conversation):
    """Additive on the preview: the list row says `kind: "program"` (the caption, never
    the card); the push says «حدّث برنامجك · برنامج القوة» in the client's language when
    the card has no caption."""
    from notifications.models import Notification

    trainer, client = pair
    routine = _program(trainer, client)
    card = _card(api, trainer, conversation, routine_id=routine.pk, day=2).json()

    row = api(client).get(f"{CHAT}/conversations/").json()['results'][0]
    assert row['last_message']['kind'] == 'program'
    assert row['last_message']['id'] == card['id']
    assert 'program' not in row['last_message']

    note = Notification.objects.get(recipient=client, event_type='chat_message')
    assert note.metadata['context']['preview'] == "حدّث برنامجك · برنامج القوة"
    assert note.metadata['data']['message_id'] == str(card['id'])


def test_a_page_of_cards_costs_what_a_page_of_two_does(api, pair, conversation):
    """Whether each card on a page may be opened is decided for the whole page at once
    (one query for a client, two for a trainer), and each card's routine comes in the
    page's own query: six cards cost what two do, for either reader."""
    from cachalot.api import cachalot_disabled

    trainer, client = pair
    routines = [_program(trainer, client, name=f"ccp page {i}") for i in range(6)]

    def cost(user):
        # Cachalot off: a cached query costs nothing the first time a page repeats it
        # and one query the next, which measures the cache, not this route.
        with cachalot_disabled(), CaptureQueriesContext(connection) as queries:
            _history(api, user, conversation)
        return len(queries)

    for routine in routines[:2]:
        assert _card(api, trainer, conversation, routine_id=routine.pk).status_code == 201
    for user in pair:
        cost(user)      # warm: a first request per user fills per-process caches
    small = {user.pk: cost(user) for user in pair}
    for routine in routines[2:]:
        assert _card(api, trainer, conversation, routine_id=routine.pk).status_code == 201
    for user in pair:
        assert cost(user) == small[user.pk], "reading cards grew with the page"
    assert all(row['program'] for row in _history(api, client, conversation))


@pytest.fixture
def socket_keeps_the_test_connection(monkeypatch):
    """`database_sync_to_async` must not close the connection this test lives in (the
    same shim as tests/test_chat.py)."""
    monkeypatch.setattr('channels.db.close_old_connections', lambda: None)


@override_settings(CHANNEL_LAYERS={
    'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}})
def test_the_socket_sends_a_card_and_carries_a_poster_when_it_lands(
        pair, conversation, socket_keeps_the_test_connection, monkeypatch):
    """One send path: a card sent over the socket is the REST card, heard by both ends
    with its `program`. And a poster the worker stores later reaches both open sockets
    as `message.updated`, the message as it now reads."""
    from channels.db import database_sync_to_async
    from channels.routing import URLRouter
    from channels.testing import WebsocketCommunicator
    from rest_framework_simplejwt.tokens import RefreshToken

    import chat.routing
    from chat import tasks
    from chat.models import KIND_VIDEO, Message

    trainer, client = pair
    routine = _program(trainer, client)
    from django.core.files.base import ContentFile

    video_row = Message.objects.create(conversation=conversation, sender=client,
                                       kind=KIND_VIDEO)
    video_row.attachment.save("video.mp4", ContentFile(_fake_mp4()), save=False)
    video_row.poster.save("poster.jpg", ContentFile(_jpeg_bytes()), save=False)
    video_row.save()

    application = URLRouter(chat.routing.websocket_urlpatterns)
    tokens = {u.pk: str(RefreshToken.for_user(u).access_token) for u in pair}

    def socket(user):
        return WebsocketCommunicator(
            application, f"/ws/chat/{conversation.pk}/?token={tokens[user.pk]}")

    async def scenario():
        trainer_socket, client_socket = socket(trainer), socket(client)
        assert (await trainer_socket.connect())[0]
        assert (await client_socket.connect())[0]
        await trainer_socket.send_to(text_data=json.dumps(
            {'type': 'message.send', 'kind': 'program', 'routine_id': routine.pk,
             'day': 2}))
        cards = [json.loads(await s.receive_from(timeout=5))
                 for s in (client_socket, trainer_socket)]
        await client_socket.send_to(text_data=json.dumps(
            {'type': 'message.send', 'kind': 'program', 'routine_id': routine.pk}))
        refused = json.loads(await client_socket.receive_from(timeout=5))
        await database_sync_to_async(tasks._announce_poster)(video_row.pk)
        updates = [json.loads(await s.receive_from(timeout=5))
                   for s in (client_socket, trainer_socket)]
        await trainer_socket.disconnect()
        await client_socket.disconnect()
        return cards, refused, updates

    cards, refused, updates = async_to_sync(scenario)()
    for frame in cards:
        assert frame['type'] == 'message.new', frame
        assert frame['message']['kind'] == 'program'
        assert frame['message']['program']['routine_id'] == routine.pk
        assert frame['message']['program']['day_name'] == "دفع"
    assert (refused['type'], refused['code']) == ('error', 'program_trainer_only')
    for frame in updates:
        assert frame['type'] == 'message.updated', frame
        assert frame['message']['id'] == video_row.pk
        assert frame['message']['media']['poster_url']


def test_an_updated_frame_keeps_its_program_card(api, pair, conversation, monkeypatch):
    """BACKEND_BUGS B-150: `message.updated` is the message AS IT NOW READS, and a
    program card reads with its card. The frame was serialized with no reader, so
    `program` was always null on it, and the app replaced the row with a card drawn
    «لم يعد متاحاً» until the next read. One frame serves both people in the thread, so
    the card rides along while BOTH may read its routine — and not once the client was
    taken off it, exactly as the client's own read of the thread answers."""
    from chat import events, tasks

    trainer, client = pair
    routine = _program(trainer, client)
    card = _card(api, trainer, conversation, routine_id=routine.pk, day=2)
    assert card.status_code == 201, card.content
    frames = []
    monkeypatch.setattr(events, 'broadcast',
                        lambda conversation_id, kind, payload:
                        frames.append((conversation_id, kind, payload)))

    tasks._announce_poster(card.json()['id'])
    [(conversation_id, kind, payload)] = frames
    assert (conversation_id, kind) == (conversation.pk, 'message.updated')
    program = payload['message']['program']
    assert program is not None, "message.updated dropped the program card"
    assert (program['routine_id'], program['day'], program['day_name']) == (
        routine.pk, 2, "دفع")

    routine.assigned_to.remove(client)
    frames.clear()
    tasks._announce_poster(card.json()['id'])
    [(_conversation_id, _kind, payload)] = frames
    assert payload['message']['program'] is None, \
        "the frame showed the client a routine they may no longer read"
    [row] = _history(api, client, conversation)
    assert row['program'] is None


# =========================================================================== POSTERS
def _jpeg_bytes(w=64, h=48):
    buffer = io.BytesIO()
    Image.new("RGB", (w, h), (200, 30, 30)).save(buffer, format="JPEG")
    return buffer.getvalue()


def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload) + 8) + kind + payload


def _fake_mp4(seconds=4.0) -> bytes:
    """An MP4 head the upload guard accepts (tests/test_chat_media.py's builder)."""
    timescale = 1000
    mvhd = (b"\x00\x00\x00\x00" + struct.pack(">I", 0) + struct.pack(">I", 0)
            + struct.pack(">I", timescale) + struct.pack(">I", int(seconds * timescale))
            + b"\x00\x01\x00\x00" + b"\x01\x00" + b"\x00" * 10 + b"\x00" * 36
            + b"\x00" * 24 + struct.pack(">I", 2))
    return (_box(b"ftyp", b"isom" + struct.pack(">I", 512) + b"isom" + b"mp42")
            + _box(b"moov", _box(b"mvhd", mvhd)) + _box(b"mdat", b"\x00" * 2048))


def _real_mp4(tmp_path, *, seconds=2.0, size="1280x720", name="real.mp4") -> bytes:
    """A real, decodable H.263-family MP4 made by the local ffmpeg (mpeg4 is built into
    every ffmpeg), with its index up front and a location tag the poster must not
    carry."""
    out = tmp_path / name
    subprocess.run(
        [shutil.which('ffmpeg'), '-v', 'error', '-y', '-f', 'lavfi', '-i',
         f'testsrc=s={size}:d={seconds}:r=10', '-pix_fmt', 'yuv420p', '-c:v', 'mpeg4',
         '-metadata', 'location=+33.5138+036.2765/', '-movflags', '+faststart',
         str(out)], check=True, capture_output=True, timeout=60)
    return out.read_bytes()


def _send_video(api, sender, conversation, content):
    return api(sender).post(MEDIA, {
        'conversation': conversation.pk, 'kind': 'video',
        'file': SimpleUploadedFile("clip.mp4", content, content_type="video/mp4")})


def _chat_files(root, prefix=""):
    found = []
    for folder, _dirs, files in os.walk(os.path.join(str(root), "chat")):
        found += [os.path.join(folder, n) for n in files if n.startswith(prefix)]
    return found


def _served(api, url) -> bytes:
    response = api().get(url)
    assert response.status_code == 200, (url, response.status_code)
    return b"".join(response.streaming_content) if response.streaming else response.content


@needs_ffmpeg
def test_a_chat_video_gets_its_poster_from_a_worker_never_from_the_request(
        api, pair, conversation, media_root, tmp_path, monkeypatch,
        django_capture_on_commit_callbacks):
    """The send stores the video and answers with `poster_url: null` — nothing on the
    request decodes a frame. Once the row commits a worker cuts one JPEG near the start
    with ffmpeg, bounded to 720 px, with no metadata, stored beside the video and served
    signed like it."""
    from chat.models import Message
    from mediakit import tasks as video_tools

    trainer, client = pair
    cuts = []
    real_cut = video_tools._poster_bytes
    monkeypatch.setattr(video_tools, "_poster_bytes",
                        lambda *a, **k: cuts.append(1) or real_cut(*a, **k))

    with django_capture_on_commit_callbacks(execute=True):
        sent = _send_video(api, client, conversation, _real_mp4(tmp_path))
        assert sent.status_code == 201, sent.content
        assert sent.json()['media']['poster_url'] is None
        assert cuts == [], "the request decoded a frame"
        assert not Message.objects.get(pk=sent.json()['id']).poster
    assert cuts == [1], "no worker cut the poster once the send committed"

    message = Message.objects.get(pk=sent.json()['id'])
    assert message.poster.name.startswith(f"chat/{conversation.pk}/poster-")
    [row] = _history(api, trainer, conversation)
    url = row['media']['poster_url']
    assert url and url.partition('?')[2].startswith('s='), "the poster is not signed"
    assert api().get(url.partition('?')[0]).status_code == 404, \
        "the bare poster path serves a private frame"
    with Image.open(io.BytesIO(_served(api, url))) as poster:
        assert poster.format == "JPEG"
        assert max(poster.size) <= 720 and poster.size == (720, 405)
        assert not poster.getexif(), "the poster carries metadata"


@needs_ffmpeg
def test_a_clip_shorter_than_a_second_still_gets_a_poster(tmp_path):
    """The frame is cut one second in; a half-second clip has none there, so the first
    frame is cut instead — for the media kit's videos and the chat's alike."""
    from mediakit import tasks as video_tools

    path = tmp_path / "short.mp4"
    path.write_bytes(_real_mp4(tmp_path, seconds=0.5, size="64x48", name="s.mp4"))
    frame = video_tools._poster_bytes(str(path))
    assert frame, "a clip shorter than a second got no poster"
    with Image.open(io.BytesIO(frame)) as poster:
        assert poster.format == "JPEG" and poster.size == (64, 48)


@needs_ffmpeg
def test_a_media_kit_video_gets_its_poster_from_the_real_ffmpeg(make_user, tmp_path):
    """`mediakit.fill_video_metadata` with the tools the image now installs: the
    poster, the measurements, `ready`."""
    from django.core.files.uploadedfile import SimpleUploadedFile as Upload

    from mediakit import tasks as video_tools
    from mediakit.models import MediaAsset
    from mediakit.video import store_video

    owner = make_user("ccp_kit_owner", user_type="trainer")
    exercise = _exercise(owner, "ccp kit press")
    asset = store_video(owner_type="exercise", owner_id=exercise.pk,
                        slot_key="exercise.loop", uploaded_by=owner,
                        upload=Upload("loop.mp4", _real_mp4(tmp_path, size="320x240"),
                                      content_type="video/mp4"))
    video_tools.fill_video_metadata(asset.pk)
    asset.refresh_from_db()
    assert asset.processing_state == MediaAsset.READY
    assert asset.poster and (asset.width, asset.height) == (320, 240)


def test_the_poster_task_is_wired_to_the_send_and_the_payload(
        api, pair, conversation, media_root, monkeypatch,
        django_capture_on_commit_callbacks):
    """Without depending on this machine's ffmpeg: a video send queues the task on
    commit, the task stores what the frame cutter gives it, and the answers carry it —
    `poster_url` on a video, null on a photo and a voice note."""
    from chat.models import Message
    from mediakit import tasks as video_tools

    trainer, client = pair
    monkeypatch.setattr(video_tools, "_poster_bytes", lambda *a, **k: _jpeg_bytes())

    with django_capture_on_commit_callbacks(execute=True):
        sent = _send_video(api, client, conversation, _fake_mp4())
    assert sent.status_code == 201, sent.content
    assert sent.json()['media']['poster_url'] is None
    message = Message.objects.get(pk=sent.json()['id'])
    assert message.poster, "the send did not queue the poster task"
    assert len(_chat_files(media_root, "poster-")) == 1

    [row] = _history(api, client, conversation)
    assert Image.open(io.BytesIO(_served(api, row['media']['poster_url']))).format \
        == "JPEG"

    # Photos and voice notes carry the key, and it is null; so is a text message's
    # media as a whole.
    from chat.tasks import cut_video_poster

    photo = api(trainer).post(MEDIA, {
        'conversation': conversation.pk, 'kind': 'image',
        'file': SimpleUploadedFile("p.jpg", _jpeg_bytes(900, 1200),
                                   content_type="image/jpeg")}).json()
    assert photo['media']['poster_url'] is None
    assert cut_video_poster(photo['id'])['state'] == 'skipped'

    # A second run of the same task changes nothing (at-least-once delivery).
    again = cut_video_poster(message.pk)
    assert again['state'] == 'skipped'
    assert len(_chat_files(media_root, "poster-")) == 1


def test_no_ffmpeg_means_no_poster_no_download_and_one_log_line(
        api, pair, conversation, monkeypatch, caplog):
    """The rule of the media kit, kept for the chat: a worker without ffmpeg stores
    nothing, downloads nothing, and says so ONCE — not once per video."""
    from chat.models import Message
    from chat.tasks import cut_video_poster
    from mediakit import tasks as video_tools

    trainer, client = pair
    ids = [_send_video(api, client, conversation, _fake_mp4()).json()['id']
           for _ in range(2)]
    monkeypatch.setattr(video_tools.shutil, "which", lambda name: None)
    monkeypatch.setattr(video_tools, "_MISSING_TOOLS", set())
    downloads = []
    monkeypatch.setattr(video_tools, "local_copy_of",
                        lambda f: downloads.append(f.name) or ("/nonexistent", False))

    with caplog.at_level(logging.WARNING, logger="mediakit.tasks"):
        results = [cut_video_poster(message_id) for message_id in ids]

    assert [r['state'] for r in results] == ['no_frame', 'no_frame']
    assert not Message.objects.filter(pk__in=ids).exclude(poster='').exists()
    assert downloads == [], "a video was downloaded with no ffmpeg to read it"
    said = [r for r in caplog.records if "ffmpeg is not installed" in r.getMessage()]
    assert len(said) == 1, [r.getMessage() for r in caplog.records]
    [row, _] = _history(api, trainer, conversation)
    assert row['media']['poster_url'] is None


def test_a_poster_is_never_written_onto_a_message_taken_back_meanwhile(
        api, pair, conversation, media_root, monkeypatch):
    """The frame is written onto the row as it is when the cut ENDS: a video its sender
    took back during the cut gets no poster, and no poster file is left behind."""
    from chat.models import Message
    from chat.services import tombstone
    from chat.tasks import cut_video_poster
    from mediakit import tasks as video_tools

    _trainer, client = pair
    sent = _send_video(api, client, conversation, _fake_mp4()).json()
    message = Message.objects.get(pk=sent['id'])

    def cut_while_taken_back(*_a, **_k):
        tombstone(Message.objects.get(pk=message.pk), client)
        return _jpeg_bytes()

    monkeypatch.setattr(video_tools, "_poster_bytes", cut_while_taken_back)
    assert cut_video_poster(message.pk)['state'] == 'superseded'
    message.refresh_from_db()
    assert not message.poster
    assert _chat_files(media_root, "poster-") == []
    # Taken back before the task ran: skipped outright.
    assert cut_video_poster(message.pk)['state'] == 'skipped'


def test_the_poster_goes_with_the_video(api, pair, conversation, media_root, monkeypatch,
                                        django_capture_on_commit_callbacks):
    """A poster is personal data derived from the video: an erasure takes it with the
    video (`chat.media.stored_names`), and a message taken back serves neither."""
    from chat.tasks import cut_video_poster
    from mediakit import tasks as video_tools
    from training_platform.privacy.registry import erase_user_data

    trainer, client = pair
    monkeypatch.setattr(video_tools, "_poster_bytes", lambda *a, **k: _jpeg_bytes())
    sent = _send_video(api, client, conversation, _fake_mp4()).json()
    cut_video_poster(sent['id'])
    assert len(_chat_files(media_root)) == 2

    gone = api(client).delete(f"{CHAT}/messages/{sent['id']}/").json()
    assert gone['media'] is None

    with django_capture_on_commit_callbacks(execute=True):
        erase_user_data(client)
    assert _chat_files(media_root) == [], "erasure left a chat video's poster in storage"


def test_the_image_installs_ffmpeg_for_the_worker():
    """The owner's job for this run: posters are cut in production, so the image the
    worker runs installs ffmpeg (a slim apt install, no recommends)."""
    dockerfile = (ROOT / "Dockerfile").read_text()
    install = re.search(r"apt-get install -y --no-install-recommends(.*?)&& rm -rf",
                        dockerfile, re.S)
    assert install, "the Dockerfile's apt install line moved"
    assert re.search(r"^\s*ffmpeg\s*\\?$", install.group(1), re.M), \
        "the image does not install ffmpeg"


def test_the_routine_route_and_the_card_read_one_rule():
    """`readable_routines` is the one definition of "who may read this routine", used
    by the routine route and by the card; a second copy in either would drift."""
    import inspect

    from chat import serializers as chat_serializers
    from routine import views as routine_views

    listing = inspect.getsource(routine_views.RoutineViewSet.get_queryset)
    assert "readable_routines(user, queryset)" in listing
    assert "readable_routines(user)" in inspect.getsource(
        chat_serializers.readable_routine_ids)

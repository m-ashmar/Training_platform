"""BACKEND_BUGS B-183: the local media route and iOS players.

AVFoundation (just_audio, video_player, Safari) asks for a byte range first and refuses
a 200 that ignores it, so against a local backend no voice note or video played; and a
`.m4a` voice note went out as `audio/mp4a-latm`, as an attachment. Production serves
Cloud Storage signed URLs, which do ranges themselves; the types are registered for
those uploads too.
"""
import mimetypes

import pytest

pytestmark = pytest.mark.django_db

BODY = bytes(range(256)) * 10          # 2560 bytes, each position recognisable


@pytest.fixture
def local_media(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)
    settings.MEDIA_URL_SIGNING = True

    def put(name):
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(BODY)
        from training_platform.media_storage import media_url
        return media_url(name)

    return put


def _body(response):
    return b"".join(response.streaming_content) if response.streaming else response.content


def test_b183_a_ranged_read_of_a_voice_note_is_206_inline_audio(api, local_media):
    url = local_media("chat/voice/note.m4a")
    resp = api().get(url, HTTP_RANGE="bytes=0-99")
    assert resp.status_code == 206, resp.status_code
    assert resp["Content-Range"] == f"bytes 0-99/{len(BODY)}"
    assert resp["Accept-Ranges"] == "bytes"
    assert _body(resp) == BODY[:100]
    assert resp["Content-Type"].startswith("audio/mp4")
    assert "attachment" not in resp.get("Content-Disposition", "")


@pytest.mark.parametrize("header,start,end", [
    ("bytes=2500-", 2500, 2559),        # open-ended
    ("bytes=-60", 2500, 2559),          # the last n bytes
    ("bytes=2550-9999", 2550, 2559),    # clipped to the end
])
def test_b183_every_single_range_form_is_honoured(api, local_media, header, start, end):
    url = local_media("chat/video/clip.mp4")
    resp = api().get(url, HTTP_RANGE=header)
    assert resp.status_code == 206
    assert resp["Content-Range"] == f"bytes {start}-{end}/{len(BODY)}"
    assert _body(resp) == BODY[start:end + 1]


def test_b183_a_range_past_the_end_is_416(api, local_media):
    url = local_media("chat/video/clip.mp4")
    resp = api().get(url, HTTP_RANGE=f"bytes={len(BODY)}-")
    assert resp.status_code == 416
    assert resp["Content-Range"] == f"bytes */{len(BODY)}"


def test_b183_no_range_is_the_whole_file_and_says_ranges_are_accepted(api, local_media):
    url = local_media("chat/video/clip.mp4")
    resp = api().get(url)
    assert resp.status_code == 200
    assert resp["Accept-Ranges"] == "bytes"
    assert _body(resp) == BODY


def test_b183_a_range_on_an_unsigned_link_is_still_refused(api, local_media):
    url = local_media("chat/voice/note.m4a")
    resp = api().get(url.partition("?")[0], HTTP_RANGE="bytes=0-1")
    assert resp.status_code == 404


@pytest.mark.parametrize("headers", [{"HTTP_RANGE": "bytes=0-3"}, {}])
def test_b183_a_path_out_of_the_media_root_is_404_not_500(api, settings, tmp_path, headers):
    """With or without a range: nothing outside MEDIA_ROOT is served, and the answer is
    a missing file (it was a 500 without one)."""
    settings.MEDIA_ROOT = str(tmp_path / "root")
    settings.MEDIA_URL_SIGNING = False
    (tmp_path / "secret.txt").write_bytes(b"not media")
    resp = api().get("/media/../secret.txt", **headers)
    assert resp.status_code == 404


def test_b183_the_voice_types_are_registered_for_uploads_too():
    assert mimetypes.guess_type("x.m4a")[0] == "audio/mp4"
    assert mimetypes.guess_type("x.ogg")[0] == "audio/ogg"

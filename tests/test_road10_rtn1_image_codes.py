"""The fix run's RTN-1, S-165 (2026-11-24): an exercise photo the upload guard refuses
answers the guard's own code. Both `validate_image` methods in `routine/serializers.py`
re-raised the guard's sentence without its code, so `field_errors.image[0].code` read
DRF's `invalid` for every refusal — a photo too big and a file that is not a photo looked
the same (S-118 had fixed the routes that answer outside a serializer).

The tests fail on the code before.
"""
import io
import os
import uuid

import pytest
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.files.uploadedfile import SimpleUploadedFile

pytestmark = pytest.mark.django_db


def _name(tag):
    return f"s165_{tag}_{uuid.uuid4().hex[:6]}"


def _noise_png(side):
    """A real PNG that does not compress: `side`² x 3 bytes of noise."""
    from PIL import Image

    out = io.BytesIO()
    Image.frombytes("RGB", (side, side), os.urandom(side * side * 3)).save(out, "PNG")
    return out.getvalue()


def test_a_photo_over_the_size_cap_says_file_too_large(make_user, api):
    from routine.models import Exercise

    trainer = make_user(_name("t"), user_type="trainer")
    photo = SimpleUploadedFile("big.png", _noise_png(1400), content_type="image/png")
    assert photo.size > 5 * 1024 * 1024
    resp = api(trainer).post("/api/routine/exercises/", {"name": _name("ex"), "image": photo})
    assert resp.status_code == 400, resp.content
    assert resp.json()["field_errors"]["image"][0]["code"] == "file_too_large"
    assert not Exercise.objects.filter(created_by=trainer).exists()


@pytest.mark.parametrize("serializer_name", ["ExerciseSerializer",
                                             "ExerciseCreateWithImageSerializer"])
def test_both_serializers_carry_the_guards_code(monkeypatch, serializer_name):
    from rest_framework import serializers

    import training_platform.file_security as guard
    from routine import serializers as module

    def refuse(*_args, **_kwargs):
        raise DjangoValidationError("Image has too many pixels.",
                                    code="image_too_many_pixels")

    monkeypatch.setattr(guard, "process_uploaded_image", refuse)
    photo = SimpleUploadedFile("p.png", _noise_png(8), content_type="image/png")
    with pytest.raises(serializers.ValidationError) as refused:
        getattr(module, serializer_name)().validate_image(photo)
    assert refused.value.get_codes() == ["image_too_many_pixels"]


def test_a_refusal_with_no_code_falls_back_to_invalid_image():
    from routine.serializers import refused_image

    assert refused_image(DjangoValidationError("odd")).get_codes() == ["invalid_image"]

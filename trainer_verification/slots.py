"""The documents a trainer sends to be verified, one slot each.

What the design draws (`yalla_gym/design/.../12-trainer-shell/TrainerVerify.html` and
`TrainerLocked.html`): the identity card, BOTH sides; at least one personal-trainer
certificate; a sports-nutrition certificate, optional ("raises your ranking"); and a
recent photo of the trainer. The card is two slots because it is two pictures and each
side can be refused on its own (a blurred back, a cut-off front).

One file per slot. The keys are the API's vocabulary (`POST documents/{slot}/`) and are
stored on every document row, so a key never changes; a new document is a new key.

`kinds` is what the BYTES may be (`trainer_verification/files.py` decides that from the
content, never from a name or a header): the card and the photo are photographs, a
certificate may also be a PDF, because that is how an issuing body sends one.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.utils.translation import gettext_lazy as _

IMAGE = "image"
PDF = "pdf"


@dataclass(frozen=True)
class DocumentSlot:
    key: str
    required: bool
    kinds: tuple[str, ...]
    label: object  # a lazy translation


SLOTS: tuple[DocumentSlot, ...] = (
    DocumentSlot("identity_front", True, (IMAGE,), _("Identity card, front")),
    DocumentSlot("identity_back", True, (IMAGE,), _("Identity card, back")),
    DocumentSlot("trainer_certificate", True, (IMAGE, PDF),
                 _("Personal trainer certificate")),
    DocumentSlot("nutrition_certificate", False, (IMAGE, PDF),
                 _("Sports nutrition certificate")),
    DocumentSlot("photo", True, (IMAGE,), _("A recent photo of you")),
)

BY_KEY = {slot.key: slot for slot in SLOTS}
REQUIRED = tuple(slot.key for slot in SLOTS if slot.required)
CHOICES = [(slot.key, slot.label) for slot in SLOTS]


def resolve(key) -> DocumentSlot | None:
    return BY_KEY.get(str(key or ""))

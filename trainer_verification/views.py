"""The trainer's own verification: `/api/v1/trainer/verification/`.

Three routes, all for the signed-in trainer and about THEIR application only — no route
takes an application or a document id, so there is nothing to walk. The operator side
(the review queue, the decision) is the console, `trainer_verification/admin.py`.

Nothing here varies by build (`X-App-Channel`): it sells nothing and moves no money.
"""
from django.utils.translation import gettext as _
from rest_framework import status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services
from .files import DocumentRefused


def _not_a_trainer():
    return Response({"error": _("Only a trainer can be verified."),
                     "code": "not_a_trainer"}, status=status.HTTP_403_FORBIDDEN)


def _refusal(exc):
    """One body for every refusal: `{error, code, ...extra}` (`missing`, `rejected`,
    `accepted`, `slot`, `slots`, `max_bytes`, `max_pixels`, `max_side`, `min_side` where
    the code says so)."""
    status_code = getattr(exc, "status", status.HTTP_400_BAD_REQUEST)
    body = {"error": exc.message, "code": exc.code}
    body.update(exc.extra)
    return Response(body, status=status_code)


class MyVerificationView(APIView):
    """`GET /api/v1/trainer/verification/` — my verification, as the app draws it
    (`TrainerVerify`, and step 3 of `TrainerLocked`). Auth: Bearer JWT, a trainer.

    200:

        {"status": "not_started" | "pending" | "rejected" | "verified",
         "round": 0,                      // how many times it was sent
         "submitted_at": ISO | null,      // the current round's
         "decided_at": ISO | null,
         "rejection_reason": str | null,  // only when status is "rejected"
         "review_business_days": 3,       // "usually within 3 business days"
         "missing": ["identity_back"],    // required slots with nothing in them
         "needs_replacing": ["photo"],    // slots whose document was refused
         "can_submit": bool,
         "url_expires_in": 300,           // seconds every URL below lives
         "documents": [                   // EVERY slot, in this order, filled or not
           {"slot": "identity_front" | "identity_back" | "trainer_certificate"
                    | "nutrition_certificate" | "photo",
            "label": "Identity card, front",       // in the request's language
            "required": true, "accepts": ["image"] | ["image", "pdf"],
            "state": "missing" | "uploaded" | "accepted" | "rejected",
            "rejection_reason": str | null,
            "kind": "image" | "pdf" | null, "content_type": str | null,
            "size": int | null, "uploaded_at": ISO | null,
            "thumbnail_url": signed URL | null,   // images only; null for a PDF
            "file_url": signed URL | null,
            "can_change": bool}]}

    `status` is `verified` whenever the account is verified, including a trainer an
    operator verified by hand before applications existed (their `documents` are all
    `missing`). The identity card is two slots (front, back); the design draws them as
    one row. `nutrition_certificate` is the one optional slot, and today it has
    no effect on roster or match order: the roster (`GET /api/v1/trainers/`) and the
    matches rank on `trainer_is_verified`, never on a document. The design labels the
    row "اختيارية · ترفع ترتيبك" ("optional · raises your ranking"); nothing does that
    yet — whether it should is the owner's decision — so the app must not promise it.

    **A revoke** (an operator withdrawing a verification, e.g. a document found forged
    afterwards) answers `status: "rejected"` with the reason, like a rejection. The
    documents it names are `rejected` with their own reason, listed in
    `needs_replacing`, and block `submit` until replaced; if it names none, every
    document is `uploaded` again (nothing stays `accepted`) and the trainer may send the
    application as it is, to be reviewed afresh.

    **URLs are signed and expire** after `url_expires_in` seconds (five minutes). Use
    them at once; read this route again rather than storing one. They are never public:
    no URL for these files is handed to anyone but this trainer and the operators.

    **What each state allows** (`can_change`, per slot): `not_started` and `rejected` —
    any slot; `pending` — only an EMPTY optional slot (the design's "أضف" on the
    nutrition certificate; the rest is being reviewed); `verified` — nothing.

    | status | `code` | when |
    |---|---|---|
    | `401` | `not_authenticated`, `token_not_valid` | no token; an expired or invalid one |
    | `403` | `not_a_trainer` | the caller is not a trainer |
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not getattr(request.user, "is_trainer", False):
            return _not_a_trainer()
        return Response(services.read(request.user, request))


class DocumentView(APIView):
    """`POST` / `DELETE /api/v1/trainer/verification/documents/{slot}/` — upload,
    replace or remove one document. Auth: Bearer JWT, a trainer.

    `{slot}` is `identity_front`, `identity_back`, `trainer_certificate`,
    `nutrition_certificate` or `photo`.

    **POST** is `multipart/form-data` with one part, `file`. One file per slot: posting
    into a filled slot REPLACES it. Answers `200` with the whole verification, exactly
    as `GET /api/v1/trainer/verification/` does, so the screen redraws from the answer.

    What the file IS is decided from its bytes, never its name or `Content-Type`
    (`trainer_verification/files.py`): a JPEG, PNG or WebP photo for every slot, or a
    PDF for the two certificates. A photo is stored re-encoded, upright and with its
    metadata (GPS included) removed. HEIC is refused — send JPEG. At most 10 MB
    (`VERIFICATION_DOCUMENT_MAX_BYTES`); a photo needs 400 px on its shorter side.

    **How large a photo may be** (what a 512 MB server can open): at most 10,000 px on
    a side. A camera JPEG may be up to 40 MP; over 16 MP it is stored at half its width
    and height (a 24 MP photo becomes 6 MP — plenty to read a card). A PNG may be up to
    16 MP, a WebP about 6.7 MP, a progressive JPEG about 8 to 13 MP depending on its
    colour format. Over that, `verification_image_too_large` with `max_pixels` (the most for
    that kind of file) and `max_side`. Sending the camera's JPEG always works.

    After a rejection, every change resets that document to `uploaded`. A document an
    operator had already decided on is kept out of sight for 90 days after it is
    replaced, then deleted; one nobody reviewed is deleted at once.

    **DELETE** empties the slot: `200` with the whole verification (also when the slot
    was already empty). Allowed when `not_started` or `rejected`.

    | status | `code` | when |
    |---|---|---|
    | `400` | `verification_file_required` | POST with no `file` part |
    | `400` | `verification_unsupported_format` | not a JPEG, PNG, WebP (or PDF where `accepts` has it), including HEIC; `accepted` lists the kinds |
    | `400` | `verification_unreadable_file` | an empty or broken file |
    | `400` | `verification_file_too_large` | over `max_bytes` |
    | `400` | `verification_image_too_large` | a photo too large to open (above): over 10,000 px on a side, or over `max_pixels` for its kind; `max_pixels`, `max_side` |
    | `400` | `verification_image_too_small` | a photo under `min_side` (400 px) on its shorter side |
    | `400` | `verification_pdf_encrypted` | a password-protected PDF |
    | `400` | `verification_pdf_active_content` | a PDF with scripts, launch actions, attachments or self-submitting forms |
    | `400` | `verification_duplicate_document` | the same file is already in another slot (`slot` names it) |
    | `401` | `not_authenticated`, `token_not_valid` | no token; an expired or invalid one |
    | `403` | `not_a_trainer` | the caller is not a trainer |
    | `404` | `verification_unknown_slot` | `{slot}` is not one of the five; `slots` lists them |
    | `409` | `verification_in_review` | the application is `pending`, and this is not an empty optional slot |
    | `409` | `verification_already_verified` | the account is verified; nothing changes now |
    | `413` | `payload_too_large` | the request body itself is over 15 MB |
    | `503` | `upload_busy` | a photo, while the server is already decoding as many images as its memory holds (`training_platform/image_budget.py`): nothing was decoded or stored; send it again after `Retry-After` seconds (`retry_after_seconds` in the body) |
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, slot):
        if not getattr(request.user, "is_trainer", False):
            return _not_a_trainer()
        try:
            services.upload_document(request.user, slot, request.FILES.get("file"))
        except (services.VerificationError, DocumentRefused) as exc:
            return _refusal(exc)
        return Response(services.read(request.user, request))

    def delete(self, request, slot):
        if not getattr(request.user, "is_trainer", False):
            return _not_a_trainer()
        try:
            services.remove_document(request.user, slot)
        except services.VerificationError as exc:
            return _refusal(exc)
        return Response(services.read(request.user, request))


class SubmitView(APIView):
    """`POST /api/v1/trainer/verification/submit/` — send the application for review.
    Auth: Bearer JWT, a trainer. No body.

    `200` with the whole verification (as `GET`), now `status: "pending"` with
    `submitted_at` set and `round` one higher. An operator reviews it in the console,
    usually within `review_business_days`; the answer arrives as the push
    `trainer_verification_approved` or `trainer_verification_rejected` (with the
    reason), and this route's `GET` shows it.

    While `pending` the trainer keeps building their library and templates: nothing
    else a trainer does waits on this.

    | status | `code` | when |
    |---|---|---|
    | `401` | `not_authenticated`, `token_not_valid` | no token; an expired or invalid one |
    | `403` | `not_a_trainer` | the caller is not a trainer |
    | `409` | `verification_documents_missing` | a required slot is empty; `missing` lists them |
    | `409` | `verification_documents_rejected` | a refused document was not replaced (or removed); `rejected` lists them |
    | `409` | `verification_already_pending` | it was sent already and is being reviewed |
    | `409` | `verification_already_verified` | the account is verified |
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [JSONParser, FormParser, MultiPartParser]

    def post(self, request):
        if not getattr(request.user, "is_trainer", False):
            return _not_a_trainer()
        try:
            services.submit(request.user)
        except services.VerificationError as exc:
            return _refusal(exc)
        return Response(services.read(request.user, request))

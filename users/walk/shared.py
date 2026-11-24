"""What several walk areas use. Every step here is a real route or service."""
from __future__ import annotations


def coach_client(walk, client, coach):
    """`client` asks `coach` and `coach` accepts, through the two routes the app uses
    (`client/request-trainer/`, `trainer/respond-to-request/`). With a coach who charges
    nothing no money moves and nothing is held; a state that needs a paid month gives
    the coach a fee and the client the money first (the money worker's states).
    Answers the approved relation."""
    from users.models import TrainerClientRelation

    walk.api(client, "post", "/api/auth/client/request-trainer/", {"trainer_id": coach.pk})
    relation = TrainerClientRelation.objects.get(trainer=coach, client=client)
    walk.api(coach, "post", "/api/auth/trainer/respond-to-request/",
             {"request_id": relation.pk, "action": "approve"})
    relation.refresh_from_db()
    client.refresh_from_db()
    return relation


def starter_exercise(walk, offset: int = 0):
    """A global catalogue exercise (`bootstrap_platform`), the `offset`-th by id."""
    from routine.models import Exercise

    return (Exercise.objects.filter(is_global=True, is_active=True)
            .exclude(catalogue_key__isnull=True).exclude(catalogue_key="")
            .order_by("id")[offset])


def sync_sets(walk, user, exercise, day, count: int, *, reps=10, weight=40,
              session=None):
    """`count` sets of `exercise` on `day`, through `POST set-logs/sync/` exactly as the
    phone sends them (its own `client_uuid` per set, the day it was trained)."""
    import uuid

    rows = []
    for number in range(1, count + 1):
        row = {"client_uuid": str(uuid.uuid4()), "exercise_id": exercise.pk,
               "set_number": number, "reps": reps, "weight": weight,
               "date": day.isoformat()}
        if session is not None:
            row["workout_session_id"] = session.pk
        rows.append(row)
    _status, body = walk.api(user, "post", "/api/routine/set-logs/sync/", {"sets": rows},
                             expect=(200, 201, 207))
    refused = [r for r in (body or {}).get("results", []) if r.get("status") == "error"]
    if refused:
        from .framework import WalkApiRefused

        raise WalkApiRefused(f"set-logs/sync/ refused {refused}")
    return body


def open_agent(walk, part="agent"):
    """An agent account with a working profile: the platform's default caps
    (`ensure_agent_profile`), switched on as an operator switches a new agent on, and not
    listed (the clients' "agents near you" list is for real shops)."""
    from wallet.models import ensure_agent_profile

    agent = walk.account(part, role="agent")
    profile = ensure_agent_profile(agent)
    profile.status = "active"
    profile.is_listed = False
    profile.save(update_fields=["status", "is_listed", "updated_at"])
    return agent


def top_up(walk, agent, client, amount: str, currency: str = "SYP"):
    """Cash taken by `agent`, credited to `client`'s wallet through the agent's own route
    (`POST wallet/agent/topup/proxy/`, money type `wallet_move`, the receivable booked).
    Answers the route's body."""
    import uuid

    _status, body = walk.api(agent, "post", "/api/wallet/agent/topup/proxy/", {
        "client_identifier": str(client.pk), "amount": amount, "currency": currency,
        "idempotency_key": f"walk-{uuid.uuid4().hex}"})
    return body


def _placeholder_png(label: str, index: int) -> bytes:
    """A PNG that says it is not an identity document. Distinct per slot (the service
    refuses the same file twice) and large enough to read."""
    import io

    from PIL import Image, ImageDraw

    image = Image.new("RGB", (900, 560), (236 + index, 240 - index, 244))
    draw = ImageDraw.Draw(image)
    for number, line in enumerate(("YALLA GYM - WALK-THROUGH ACCOUNT", "",
                                   "NOT AN IDENTITY DOCUMENT", f"Placeholder for: {label}")):
        draw.text((60, 120 + 50 * number), line, fill=(40, 40, 40))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def verify_trainer(walk, trainer, operator):
    """The trainer's documents through the real upload route (multipart, one per required
    slot), the submission, and an operator's decision (`trainer_verification.services
    decide`, the console's). The files are placeholders that say so; under the shared
    test fixture they land in the test's own media folder."""
    from django.core.files.uploadedfile import SimpleUploadedFile

    from trainer_verification import services, slots

    for index, slot in enumerate(slots.REQUIRED):
        upload = SimpleUploadedFile(f"{slot}.png", _placeholder_png(slot, index),
                                    content_type="image/png")
        walk.api(trainer, "post", f"/api/v1/trainer/verification/documents/{slot}/",
                 {"file": upload}, multipart=True)
    walk.api(trainer, "post", "/api/v1/trainer/verification/submit/", {})
    application = services.application_of(trainer)
    services.decide(application.pk, operator, approve=True,
                    reason="Walk-through account: placeholder documents that say so.")
    trainer.refresh_from_db()
    return application

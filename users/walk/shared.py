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
    """The `offset`-th exercise of the starter catalogue (`routine/data/
    exercise_catalogue.py STARTER_EXERCISES`, in its own order), which `bootstrap_platform`
    writes and the command checks before any state. Not "the n-th global exercise by
    id": a developer's database holds other catalogue rows, and which one came n-th
    would change from one database to the next."""
    from routine.data.exercise_catalogue import STARTER_EXERCISES
    from routine.models import Exercise

    return Exercise.objects.get(catalogue_key=STARTER_EXERCISES[offset].key)


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


def new_wallet_currency() -> str:
    """The currency a wallet takes when nothing has said otherwise: the platform's base
    currency (`wallet.models.currency_for`; settings `PLATFORM_CURRENCY`). A walk account
    is new, so it trades in this one until a state tops it up in another."""
    from django.conf import settings

    return getattr(settings, "PLATFORM_CURRENCY", "SYP")


def plans_on_sale(currency: str, *, diet: bool = False):
    """The paid plans on sale PRICED IN `currency`, cheapest first (ties by id). A price
    is only ever compared with another in the same currency: 12 dollars is not cheaper
    than 1000 pounds, and pricing one in the other needs an operator's rate, which a
    state never sets."""
    from subscription.models import SubscriptionPlan

    plans = SubscriptionPlan.objects.filter(is_active=True, price__gt=0, currency=currency)
    if diet:
        plans = plans.filter(has_diet_access=True)
    return plans.order_by("price", "id")


def missing_plan(currency: str | None = None, *, diet: bool = False) -> list[str]:
    """For an area's `@needs`: `[]` when a plan is on sale priced in `currency` (a new
    wallet's when None), with the diet if `diet`; else the one gap, in the same words
    for every area that needs it (the refusal names each gap once)."""
    currency = currency or new_wallet_currency()
    if plans_on_sale(currency, diet=diet).exists():
        return []
    return [f"a plan on sale{' with the diet' if diet else ''}, priced in {currency} "
            f"(the currency a new wallet takes)"]


def plan_for(client, *, diet: bool = False, prefer: str | None = None):
    """A plan `client` can pay from their own wallet without an exchange rate: on sale,
    priced in the client's wallet currency (`currency_for`), with the diet if `diet`.
    The catalogue's `prefer` plan (`catalogue_key`) when it is one of those, else the
    cheapest of them. Raises `WalkRefused` naming what is missing; the command's
    pre-flight check (`@needs`) names it before any state runs."""
    from wallet import currency_for

    from .framework import WalkRefused

    currency = currency_for(client)
    plans = plans_on_sale(currency, diet=diet)
    plan = (plans.filter(catalogue_key=prefer).first() if prefer else None) or plans.first()
    if plan is None:
        raise WalkRefused(f"no plan on sale{' with the diet' if diet else ''} priced in "
                          f"{currency} (the client's wallet currency)")
    return plan


def top_up(walk, agent, client, amount: str, currency: str | None = None):
    """Cash taken by `agent`, credited to `client`'s wallet through the agent's own route
    (`POST wallet/agent/topup/proxy/`, money type `wallet_move`, the receivable booked),
    in the client's own currency unless `currency` says otherwise (`currency_for`: a new
    wallet's is the platform's base currency). Answers the route's body."""
    import uuid

    from wallet import currency_for

    currency = currency or currency_for(client)
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

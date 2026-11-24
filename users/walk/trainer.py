"""Walk states: trainer and products (`users/walk/__init__.py` says how to add one).

Owned by the batch B worker. A trainer's verification goes through the app's own
routes (each document uploaded, the application sent) and an operator's decision
through the service the console calls (`trainer_verification/services.py decide`). A
product is drafted and sent through the trainer's product routes, then approved or
returned by an operator (`routine/products.py approve` / `reject`, the console's). The
trainer's template and the operator come from `users/walk/training.py`.
"""
from __future__ import annotations

from . import state
from .framework import on_retire
from .training import PRODUCT_PRICE, THREE_A_WEEK, template, the_operator

#: What the operator writes when the verification is returned: the trainer reads it.
RETURNED_REASON = "صورة الهوية غير واضحة: أعد تصويرها في ضوء جيد ومن دون انعكاس."

#: What the operator writes when a product is returned: the trainer reads it.
PRODUCT_RETURNED_REASON = "الوصف قصير جداً: اشرح لمن البرنامج وما يحتاجه من معدات."


#: Why a retired walk trainer's waiting work is taken off the operator's desk.
RESET_REASON = "Walk-through account reset (seed_walk_states)."


@on_retire
def _clear_the_operators_desk_of_a_retired_walk_trainer(users):
    """A verification application and a product wait on the operator's desk (the
    console's queues, the operator phone's counts) whoever sent them, so every run of
    `trainer_verification_in_review` and `trainer_products` would leave one more of
    each. Before the accounts are retired, each is decided as an operator decides one:
    a pending application returned (`trainer_verification.services.decide`), a product
    still in review returned to draft (`routine.products.reject`), the reason saying
    why. Walk data only — `users` are walk accounts being retired; the decider is the
    retiring state's own operator when it has one."""
    from routine import products
    from routine.product_models import ProductStatus, RoutineProduct
    from trainer_verification import services
    from trainer_verification.models import ApplicationState, VerificationApplication

    decider = next((user for user in users if getattr(user, "user_type", "") == "admin"),
                   None)
    for application in VerificationApplication.objects.filter(
            trainer__in=users, state=ApplicationState.PENDING):
        services.decide(application.pk, decider, approve=False, reason=RESET_REASON)
    for product_id in RoutineProduct.objects.filter(
            trainer__in=users, status=ProductStatus.IN_REVIEW).values_list("pk", flat=True):
        products.reject(product_id, decider, RESET_REASON)


# ------------------------------------------------------------------ helpers
def send_for_review(walk, trainer):
    """Every required document through the trainer's upload route, then the
    application sent (`verification/submit/`): it waits for an operator. The files are
    the shared placeholders that say they are not identity documents."""
    from django.core.files.uploadedfile import SimpleUploadedFile

    from trainer_verification import services, slots

    from .shared import _placeholder_png

    for index, slot in enumerate(slots.REQUIRED):
        upload = SimpleUploadedFile(f"{slot}.png", _placeholder_png(slot, index),
                                    content_type="image/png")
        walk.api(trainer, "post", f"/api/v1/trainer/verification/documents/{slot}/",
                 {"file": upload}, multipart=True)
    walk.api(trainer, "post", "/api/v1/trainer/verification/submit/", {})
    return services.application_of(trainer)


def verified(walk, trainer):
    """The trainer verified: the documents and the submission through the routes, and an
    operator's approval (`shared.verify_trainer`)."""
    from .shared import verify_trainer

    if not trainer.trainer_is_verified:
        verify_trainer(walk, trainer, the_operator(walk))
    return trainer


def draft_product(walk, trainer, *, name, goal="strength", weeks=4, price=PRODUCT_PRICE):
    """A template of three sessions a week and a product drafted from it, through the
    trainer's own routes (`POST templates/`, `POST /api/v1/trainer/products/`), priced
    `PRODUCT_PRICE` (what `training.bought_programme_missed_day` declares it pays).
    Answers the product's id."""
    source = template(walk, trainer, name=name, goal=goal, days=3, weeks=weeks,
                      week=THREE_A_WEEK)
    _status, body = walk.api(trainer, "post", "/api/v1/trainer/products/", {
        "source_template_id": source, "title_ar": name, "title_en": name,
        "description_ar": "برنامج مكتوب بعناية لثلاثة أيام في الأسبوع.",
        "description_en": "A careful three-day-a-week programme.",
        "price": f"{price:.2f}", "preview_days": 1, "place": "gym"})
    return body["id"]


def send_product(walk, trainer, product_id):
    """The product sent to the operators' review (`.../submit/`)."""
    walk.api(trainer, "post", f"/api/v1/trainer/products/{product_id}/submit/", {})


def product_on_sale(walk, trainer, *, name="ثلاثة أيام للقوة"):
    """A verified trainer's product, sent and approved by an operator: on sale."""
    from routine import products
    from routine.product_models import RoutineProduct

    verified(walk, trainer)
    product_id = draft_product(walk, trainer, name=name)
    send_product(walk, trainer, product_id)
    products.approve(product_id, the_operator(walk))
    return RoutineProduct.objects.get(pk=product_id)


# ------------------------------------------------------------------ 3. verification
@state("trainer_verification_returned", area="trainer", role="trainer",
       summary="A trainer whose verification an operator RETURNED with remarks: the "
               "application `rejected`, a reason for it and for one document.")
def trainer_verification_returned(walk):
    from trainer_verification import services
    from trainer_verification.models import VerificationDocument

    me = walk.account()
    application = send_for_review(walk, me)
    document = (VerificationDocument.objects.filter(application=application)
                .order_by("id").first())
    services.decide(application.pk, the_operator(walk), approve=False,
                    reason=RETURNED_REASON, document_reasons={document.pk: RETURNED_REASON})
    walk.ids(application=application.pk, returned_document=document.pk)


@state("trainer_verification_in_review", area="trainer", role="trainer",
       summary="A trainer whose documents are sent and waiting for an operator.")
def trainer_verification_in_review(walk):
    me = walk.account()
    application = send_for_review(walk, me)
    walk.ids(application=application.pk)


# ------------------------------------------------------------------ 4. products
@state("trainer_products", area="trainer", role="trainer",
       summary="A verified trainer with three products: one on sale, one in review, "
               "one returned with the operator's reason.")
def trainer_products(walk):
    from routine import products

    me = walk.account()
    on_sale = product_on_sale(walk, me)
    waiting = draft_product(walk, me, name="لياقة في البيت", goal="fat_loss")
    send_product(walk, me, waiting)
    returned = draft_product(walk, me, name="عضل في ستة أسابيع", goal="build_muscle",
                             weeks=6)
    send_product(walk, me, returned)
    products.reject(returned, the_operator(walk), PRODUCT_RETURNED_REASON)
    walk.ids(product_on_sale=on_sale.pk, product_in_review=waiting,
             product_returned=returned)

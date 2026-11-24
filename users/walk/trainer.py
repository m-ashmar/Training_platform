"""Walk states: trainer and products (`users/walk/__init__.py` says how to add one).

Owned by the batch B worker. A trainer's verification goes through
`trainer_verification/services.py` (the documents, the application, an operator's
decision), a product through the trainer's own write serializer and
`routine/products.py` (`submit`, then an operator's `approve` or `reject`). The shared
pieces — the trainer's template, an agent top-up, a coaching — are
`users/walk/training.py`'s.
"""
from __future__ import annotations

from . import state
from .training import (draft_product, product_on_sale, send_for_review, template,
                       the_operator, verified_trainer)

#: What the operator writes when the verification is returned: the trainer reads it.
RETURNED_REASON = "صورة الهوية غير واضحة: أعد تصويرها في ضوء جيد ومن دون انعكاس."


@state("trainer_verification_returned", area="trainer", role="trainer",
       summary="A trainer whose verification an operator RETURNED with remarks: the "
               "application `rejected`, a reason for it and for one document.")
def trainer_verification_returned(walk):
    from trainer_verification import services
    from trainer_verification.models import VerificationDocument

    me = walk.account()
    application = send_for_review(me)
    document = (VerificationDocument.objects.filter(application=application)
                .order_by("id").first())
    services.decide(application.pk, the_operator(walk), approve=False,
                    reason=RETURNED_REASON,
                    document_reasons={document.pk: RETURNED_REASON} if document else None)
    walk.ids(application=application.pk,
             returned_document=document.pk if document else None)


@state("trainer_verification_in_review", area="trainer", role="trainer",
       summary="A trainer whose documents are sent and waiting for an operator.")
def trainer_verification_in_review(walk):
    me = walk.account()
    application = send_for_review(me)
    walk.ids(application=application.pk)


#: What the operator writes when a product is returned: the trainer reads it.
PRODUCT_RETURNED_REASON = "الوصف قصير جداً: اشرح لمن البرنامج وما يحتاجه من معدات."


@state("trainer_products", area="trainer", role="trainer",
       summary="A verified trainer with three products: one in review, one on sale, "
               "one returned with the operator's reason.")
def trainer_products(walk):
    from routine import products
    from users.walk.training import THREE_A_WEEK

    me = walk.account()
    verified_trainer(walk, me)
    on_sale = product_on_sale(walk, me, title_ar="ثلاثة أيام للقوة",
                              title_en="Three days of strength")
    waiting = draft_product(
        me, template(me, name_ar="لياقة في البيت", name_en="Fitness at home", days=3,
                     weeks=4, week=THREE_A_WEEK, goal="fat_loss"),
        title_ar="لياقة في البيت", title_en="Fitness at home")
    products.submit(me, waiting.pk)
    returned = draft_product(
        me, template(me, name_ar="عضل في ستة أسابيع", name_en="Muscle in six weeks",
                     days=3, weeks=6, week=THREE_A_WEEK, goal="build_muscle"),
        title_ar="عضل في ستة أسابيع", title_en="Muscle in six weeks")
    products.submit(me, returned.pk)
    products.reject(returned.pk, the_operator(walk), PRODUCT_RETURNED_REASON)
    walk.ids(product_on_sale=on_sale.pk, product_in_review=waiting.pk,
             product_returned=returned.pk)

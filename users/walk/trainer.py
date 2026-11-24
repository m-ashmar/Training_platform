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


# ------------------------------------------------------------------ 7, 8: a trainer's day
@state("trainer_brand_new", area="trainer", role="trainer",
       summary="A trainer who has just signed up: no name, not verified, not taking "
               "clients, no template, no client.")
def trainer_brand_new(walk):
    walk.account(onboarded=False, trainer_is_available=False)


#: What the client writes and the trainer has not answered yet.
UNANSWERED = "مرحباً كابتن، أحسّ بألم خفيف في الكتف بعد تمرين الضغط. هل أكمل؟"


@state("trainer_busy_day", area="trainer", role="trainer",
       summary="A verified trainer with a request to accept, a client waiting for a first "
               "programme, an unanswered message and a finished session to review.")
def trainer_busy_day(walk):
    from django.utils import timezone

    from chat.services import open_conversation, send_message

    from .training import ask, coach, fund, programme, train

    me = walk.account()
    verified_trainer(walk, me)
    today = timezone.localdate()

    # A client whose month 1 is held and who has no programme yet.
    waiting = walk.account("waiting", first_name="ليلى", last_name="درويش", gender="Female")
    waiting_relation = coach(walk, me, waiting)

    # A coached client who wrote, and nobody has answered.
    talker = walk.account("talker", first_name="كريم", last_name="نجار")
    coach(walk, me, talker)
    talker_routine = programme(me, talker, start=today, weeks=4,
                               name_ar="بداية قوية · 4 أسابيع", name_en="Strong start · 4 weeks")
    conversation = open_conversation(me, talker)
    message, _created = send_message(conversation, talker, {"body": UNANSWERED})

    # A coached client who has just finished a workout: the trainer is told, and the
    # session is there to review.
    finisher = walk.account("finisher", first_name="سامر", last_name="خوري")
    coach(walk, me, finisher)
    finisher_routine = programme(me, finisher, start=today, weeks=4,
                                 name_ar="قوة الجسم كاملاً", name_en="Full body strength")
    session = train(finisher, finisher_routine, 1, today, finished_now=True)

    # A client who has asked and is waiting for the answer (the fee in the wallet).
    asker = walk.account("asker", first_name="مايا", last_name="حلبي", gender="Female")
    fund(walk, asker, me.trainer_hourly_rate)
    request = ask(me, asker, note="أريد خسارة الوزن قبل الصيف، أتمرن في البيت.")

    walk.ids(request=request.pk, waiting_client=waiting.pk,
             waiting_relation=waiting_relation.pk, talker=talker.pk,
             conversation=conversation.pk,
             message=message.pk, talker_routine=talker_routine.pk,
             finisher=finisher.pk, session=session.pk, asker=asker.pk)

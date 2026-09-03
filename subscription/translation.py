"""Arabic for the things a subscriber reads before they pay.

The platform serves `en` and `ar` and eight models were registered for translation;
`SubscriptionPlan` was not one of them, so there was no `name_ar` or `description_ar`
to fill and the plan list came back byte-identical in both languages. That list is the
last screen before a payment.

`SubscriptionFeature.name` is deliberately left alone: it is the lookup key that
`subscription.quota` resolves features by, not display text, and translating it would
make `get(name="daily_meals")` resolve against whatever language happened to be active.
"""
from modeltranslation.translator import TranslationOptions, register

from .models import SubscriptionPlan


@register(SubscriptionPlan)
class SubscriptionPlanTranslationOptions(TranslationOptions):
    fields = ("name", "description")

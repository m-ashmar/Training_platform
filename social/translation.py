from modeltranslation.translator import register, TranslationOptions
from .models import Challenge, Achievement


@register(Challenge)
class ChallengeTranslationOptions(TranslationOptions):
    fields = ('title', 'description',)


# NOTE: This is social.models.Achievement, distinct from achievements.models.Achievement
@register(Achievement)
class SocialAchievementTranslationOptions(TranslationOptions):
    fields = ('name', 'description',)

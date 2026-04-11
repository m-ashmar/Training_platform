from modeltranslation.translator import register, TranslationOptions
from .models import Achievement

@register(Achievement)
class AchievementTranslationOptions(TranslationOptions):
    fields = ('name', 'description',)

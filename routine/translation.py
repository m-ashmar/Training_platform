from modeltranslation.translator import register, TranslationOptions
from .models import Exercise, RoutineTemplate

@register(Exercise)
class ExerciseTranslationOptions(TranslationOptions):
    fields = ('name', 'description',)

@register(RoutineTemplate)
class RoutineTemplateTranslationOptions(TranslationOptions):
    fields = ('name', 'description',)

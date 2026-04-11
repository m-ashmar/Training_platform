from modeltranslation.translator import register, TranslationOptions
from .models import FoodCategory, FoodItem, DietPlanTemplate

@register(FoodCategory)
class FoodCategoryTranslationOptions(TranslationOptions):
    fields = ('name',)

@register(FoodItem)
class FoodItemTranslationOptions(TranslationOptions):
    fields = ('name',)
    
@register(DietPlanTemplate)
class DietPlanTemplateTranslationOptions(TranslationOptions):
    fields = ('name', 'description',)

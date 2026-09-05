from modeltranslation.translator import register, TranslationOptions
from .models import FoodCategory, FoodItem, DietPlanTemplate, Recipe

@register(FoodCategory)
class FoodCategoryTranslationOptions(TranslationOptions):
    fields = ('name',)

@register(FoodItem)
class FoodItemTranslationOptions(TranslationOptions):
    fields = ('name',)
    
@register(DietPlanTemplate)
class DietPlanTemplateTranslationOptions(TranslationOptions):
    fields = ('name', 'description',)


@register(Recipe)
class RecipeTranslationOptions(TranslationOptions):
    fields = ('name', 'description')

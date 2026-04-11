from rest_framework import serializers
from django.utils import translation

class TranslatedJSONFieldMixin(serializers.Serializer):
    """
    Serializer mixin to dynamically extract translated fields from a JSONField
    based on the current active language, reducing payload size by dropping 
    the full translations dictionary.
    
    Usage:
    class MySerializer(TranslatedJSONFieldMixin, serializers.ModelSerializer):
        translated_fields = ['name', 'description']
        translations_field_name = 'translations' # Default
        
        class Meta:
            model = MyModel
            fields = ['id', 'name', 'description', 'other_field']
    """
    
    def to_representation(self, instance):
        # the base representation
        ret = super().to_representation(instance)
        
        # Determine the current language (fallback to 'en' if not yet activated)
        current_lang = translation.get_language() or 'en'
        
        translated_fields = getattr(self, 'translated_fields', [])
        translations_field = getattr(self, 'translations_field_name', 'translations')
        
        # If english, or no translations available, return as-is
        if current_lang == 'en' or not hasattr(instance, translations_field) or not getattr(instance, translations_field):
            # remove the full translations payload from frontend
            if translations_field in ret:
                ret.pop(translations_field, None)
            return ret
            
        translations = getattr(instance, translations_field)
        if not isinstance(translations, dict):
            if translations_field in ret:
                ret.pop(translations_field, None)
            return ret
            
        lang_data = translations.get(current_lang, {})
        
        # Inject translations
        for field in translated_fields:
            if field in lang_data and lang_data[field]:
                # Overwrite the base english field with the localized version
                ret[field] = lang_data[field]
                
        # Clean up the raw translations payload to save bandwidth
        if translations_field in ret:
            ret.pop(translations_field, None)
            
        return ret

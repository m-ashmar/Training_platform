"""
Management command to set up initial diet plan templates.
"""

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import translation

from diet.models import DietPlanTemplate

#: The Arabic a trainer reads in the template picker, by English name. Written on
#: create, and onto an existing row only where its Arabic is still EMPTY, so an
#: operator's wording is never replaced. The app is Arabic-first; these six were English
#: in both languages.
ARABIC = {
    '3 Meals + 1 Snack': ('3 وجبات + وجبة خفيفة',
                          'ثلاث وجبات رئيسية ووجبة خفيفة، مناسبة لمعظم المستخدمين'),
    '4 Meals + 2 Snacks': ('4 وجبات + وجبتان خفيفتان',
                           'أربع وجبات ووجبتان خفيفتان، مثالية للنشيطين ولبناء العضل'),
    '5 Meals + 1 Snack': ('5 وجبات + وجبة خفيفة',
                          'خمس وجبات ووجبة خفيفة، للمتقدمين ولكمال الأجسام'),
    '6 Meals + 2 Snacks': ('6 وجبات + وجبتان خفيفتان',
                           'ست وجبات ووجبتان خفيفتان، للرياضيين المحترفين واللياقة العالية'),
    '3 Meals Only': ('3 وجبات فقط', 'ثلاث وجبات يومياً، للمبتدئين والمتابعة البسيطة'),
    '4 Meals Only': ('4 وجبات فقط', 'أربع وجبات يومياً، نهج متوازن للنشاط المعتدل'),
}


class Command(BaseCommand):
    help = 'Set up initial diet plan templates'

    def handle(self, *args, **options):
        # The default language, whatever locale called this (`bootstrap_platform`
        # does): `name=` and `description=` below write the ACTIVE language's column.
        with translation.override(settings.LANGUAGE_CODE):
            self._handle()

    def _handle(self):
        self.stdout.write('Setting up diet plan templates...')
        
        templates_data = [
            {
                'name': '3 Meals + 1 Snack',
                'description': 'Standard 3 meals with 1 snack - perfect for most users',
                'meals_per_day': 3,
                'snacks_per_day': 1,
                'days_variation': 1
            },
            {
                'name': '4 Meals + 2 Snacks',
                'description': '4 meals with 2 snacks - ideal for active users and muscle building',
                'meals_per_day': 4,
                'snacks_per_day': 2,
                'days_variation': 2
            },
            {
                'name': '5 Meals + 1 Snack',
                'description': '5 meals with 1 snack - for advanced users and bodybuilding',
                'meals_per_day': 5,
                'snacks_per_day': 1,
                'days_variation': 3
            },
            {
                'name': '6 Meals + 2 Snacks',
                'description': '6 meals with 2 snacks - for professional athletes and extreme fitness',
                'meals_per_day': 6,
                'snacks_per_day': 2,
                'days_variation': 4
            },
            {
                'name': '3 Meals Only',
                'description': 'Simple 3 meals per day - for beginners and simple tracking',
                'meals_per_day': 3,
                'snacks_per_day': 0,
                'days_variation': 1
            },
            {
                'name': '4 Meals Only',
                'description': '4 meals per day - balanced approach for moderate activity',
                'meals_per_day': 4,
                'snacks_per_day': 0,
                'days_variation': 2
            }
        ]
        
        created_count = 0
        for template_data in templates_data:
            # Keyed on `name_en`. `name` is a modeltranslation virtual field and
            # `DietPlanTemplate` has no unique key at all, so running this under an
            # Arabic locale on a database already seeded in English created a SECOND set
            # of six — twelve indistinguishable rows in the trainer's picker, six of them
            # answering '' for `.name` under English because the English text had gone
            # into the Arabic column. `FoodCategory.by_canonical_name` solves exactly
            # this and this model never got it.
            template = DietPlanTemplate.objects.filter(
                name_en=template_data['name']).first()
            created = template is None
            name_ar, description_ar = ARABIC[template_data['name']]
            if created:
                template = DietPlanTemplate.objects.create(
                    name_en=template_data['name'],
                    description_en=template_data['description'],
                    name_ar=name_ar, description_ar=description_ar, **template_data)
            if created:
                created_count += 1
                self.stdout.write(f'  Created template: {template.name}')
                continue
            arabic = {'name_ar': name_ar, 'description_ar': description_ar}
            filled = [field for field in arabic
                      if not (getattr(template, field) or '').strip()]
            for field in filled:
                setattr(template, field, arabic[field])
            if filled:
                template.save(update_fields=filled)
                self.stdout.write(f'  Template already exists, Arabic added: {template.name}')
            else:
                self.stdout.write(f'  Template already exists: {template.name}')
        
        self.stdout.write(
            self.style.SUCCESS(f'Successfully set up {created_count} new templates. '
                             f'Total templates: {DietPlanTemplate.objects.count()}')
        ) 
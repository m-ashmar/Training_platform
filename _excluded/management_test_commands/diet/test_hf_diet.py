from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from django.conf import settings
import requests
from diet.ai_services import DietGenerator


class Command(BaseCommand):
    help = "Generate a sample diet plan using the configured Hugging Face provider for a given user id"

    def add_arguments(self, parser):
        parser.add_argument('--user-id', type=int, required=True)
        parser.add_argument('--meals', type=int, default=3)
        parser.add_argument('--snacks', type=int, default=0)

    def handle(self, *args, **options):
        user_id = options['user_id']
        meals = options['meals']
        snacks = options['snacks']

        User = get_user_model()
        user = User.objects.get(id=user_id)

        generator = DietGenerator(user)
        try:
            output = generator.generate_plan(meal_count=meals, snack_count=snacks)
            self.stdout.write(self.style.SUCCESS(f"Generated {len(output.plan)} meals. Metadata: {output.generation_metadata}"))
        except Exception as e:
            # Fallback: call HF REST directly and print raw output to validate connectivity
            self.stdout.write(self.style.WARNING(f"Structured generation failed: {str(e)}. Falling back to raw HF output..."))
            # Rebuild prompt like DietGenerator
            user_data = generator._get_user_data(meals, snacks)
            data_for_prompt = dict(user_data)
            data_for_prompt.pop('meal_count', None)
            data_for_prompt.pop('snack_count', None)
            from langchain_core.prompts import PromptTemplate
            prompt = PromptTemplate(
                template=generator.prompt_template,
                input_variables=["meal_count", "snack_count"],
                partial_variables=data_for_prompt
            )
            final_prompt = prompt.format(meal_count=meals, snack_count=snacks)
            model = getattr(settings, 'HF_MODEL_REPO', 'gpt2')
            token = getattr(settings, 'HUGGINGFACE_API_TOKEN', '')
            api_url = f"https://api-inference.huggingface.co/models/{model}"
            headers = {"Authorization": f"Bearer {token}"}
            payload = {
                "inputs": final_prompt,
                "parameters": {
                    "temperature": 0.7,
                    "max_new_tokens": 512,
                    "return_full_text": False
                }
            }
            resp = requests.post(api_url, headers=headers, json=payload, timeout=60)
            raw = resp.text
            self.stdout.write(self.style.SUCCESS("Raw HF response:"))
            self.stdout.write(raw)



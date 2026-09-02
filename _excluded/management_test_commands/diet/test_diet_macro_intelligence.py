from django.core.management.base import BaseCommand
from django.conf import settings
from diet.models import FoodItem
from diet.utils.nutrition import get_macro_densities_for_food, macro_efficiency_score


class Command(BaseCommand):
    help = "Print top 5 foods by smart efficiency per goal for debug"

    def add_arguments(self, parser):
        parser.add_argument('--goal', type=str, default='Maintain', help='Lose|Maintain|Gain')

    def handle(self, *args, **options):
        goal = options['goal']
        foods = list(FoodItem.objects.all()[:200])
        scored = []
        for f in foods:
            p_pg, c_pg, f_pg, kcal_pg = get_macro_densities_for_food(f)
            if kcal_pg <= 0:
                continue
            eff = macro_efficiency_score(p_pg, c_pg, f_pg, goal)
            try:
                eff *= float(getattr(f, 'smart_score_weight', 1.0) or 1.0)
            except Exception:
                pass
            score = eff / max(kcal_pg, 1e-9)
            scored.append((score, f.name, round(p_pg,3), round(c_pg,3), round(f_pg,3), round(kcal_pg,3)))
        scored.sort(reverse=True)
        top = scored[:5]
        self.stdout.write(self.style.SUCCESS(f"Top 5 foods for goal={goal}:"))
        for s in top:
            self.stdout.write(f"score={round(s[0],4)} name={s[1]} p_pg={s[2]} c_pg={s[3]} f_pg={s[4]} kcal_pg={s[5]}")





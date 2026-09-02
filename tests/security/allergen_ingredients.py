import os, sys, django, logging
sys.path.insert(0,'/Users/mac/Desktop/Git/t2/Training_platform')
os.environ['DJANGO_SETTINGS_MODULE']='training_platform.settings_local'
django.setup()
from django.test.utils import get_runner; from django.conf import settings
r=get_runner(settings)(interactive=False); old=r.setup_databases()
logging.disable(logging.ERROR)
from diet.models import FoodItem, FoodCategory
from diet.services.meal_validator import AllergenChecker, MealValidator, VIOLATION, UNVERIFIED, SAFE
cat=FoodCategory.objects.create(name='c')
def food(name, allergens=None, source='unknown', ingredients=''):
    return FoodItem.objects.create(name=name, category=cat, allergens=allergens or [],
                                   allergen_source=source, ingredients_text=ingredients)
res=[]
def chk(label, cond): res.append((label,cond))

# 1. THE ORIGINAL BUG: a naturally written list must block every listed allergen
pb  = food('Peanut butter', ['peanut'], 'verified')
sh  = food('Shellfish platter', ['shellfish'], 'verified')
mc  = food('Milk chocolate', ['milk'], 'verified')
ck  = food('Grilled chicken', [], 'verified')
c1  = AllergenChecker('peanuts, shellfish, milk')
verdicts={v.food_name: v.verdict for v in c1.check_foods([pb,sh,mc,ck]).verdicts}
chk("peanut butter blocked",   verdicts['Peanut butter']==VIOLATION)
chk("shellfish blocked",       verdicts['Shellfish platter']==VIOLATION)
chk("milk chocolate blocked",  verdicts['Milk chocolate']==VIOLATION)
chk("safe food passes",        verdicts['Grilled chicken']==SAFE)

# 2. THE POINT OF THE REWRITE: composition, not the name
pad = food('Pad Thai', ['peanut','egg','fish'], 'verified',
           ingredients='rice noodles, peanuts, egg, fish sauce, tamarind')
v = AllergenChecker('peanut').check_food(pad)
chk("Pad Thai caught by INGREDIENTS (name says nothing)", v.verdict==VIOLATION and 'peanut' in v.matched)

# 3. No false positives
egp = food('Eggplant parmesan', ['milk','gluten'], 'verified')
chk("Eggplant NOT flagged for an egg allergy", AllergenChecker('egg').check_food(egp).verdict!=VIOLATION)
coc = food('Coconut water', [], 'verified')
chk("Coconut NOT flagged for a nut allergy",   AllergenChecker('nuts').check_food(coc).verdict!=VIOLATION)

# 4. UNKNOWN IS NOT SAFE — the core requirement
mys = food('Mystery stew')            # source='unknown'
mv  = AllergenChecker('peanut').check_food(mys)
chk("food with no allergen data -> UNVERIFIED, not safe", mv.verdict==UNVERIFIED)

# 5. The system is AWARE — a report exists so action can be taken
val=MealValidator('peanuts, shellfish')
kept=[f.name for f,_ in val.validate([(pb,'1'),(sh,'1'),(ck,'1'),(mys,'1')])]
rep=val.report
chk("violations are reported, not just filtered", len(rep.violations)==2)
chk("unverified ingredient surfaced",            len(rep.unverified)==1)
chk("report.is_safe is False when anything is unknown", rep.is_safe is False)
chk("violating foods removed from the meal",     'Peanut butter' not in kept and 'Shellfish platter' not in kept)
chk("safe food retained",                        'Grilled chicken' in kept)

# 6. no allergies declared -> nothing blocked
chk("no allergies -> everything passes", len([f for f,_ in MealValidator('').validate([(pb,'1'),(sh,'1')])])==2)

for k,v in res: print(f"  [{'PASS' if v else 'FAIL'}] {k}")
print(f"\n{sum(1 for _,v in res if v)}/{len(res)} PASS")
print("\n  sample report:", rep.as_dict())
r.teardown_databases(old)

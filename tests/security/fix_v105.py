import os, sys, django, logging
sys.path.insert(0,'/Users/mac/Desktop/Git/t2/Training_platform')
os.environ['DJANGO_SETTINGS_MODULE']='training_platform.settings_local'
django.setup()
from django.test.utils import get_runner; from django.conf import settings
r=get_runner(settings)(interactive=False); old=r.setup_databases()
logging.disable(logging.CRITICAL)
from users.models import CustomUser
from diet.models import FoodItem, FoodCategory, UserFoodPreference
from diet.services.rule_based_planner import RuleBasedPlanner
cat=FoodCategory.objects.create(name='c')
def mk(n,**kw): return FoodItem.objects.create(name=n, category=cat, api_id=n, calories=200,
    protein=20, carbs=10, fat=5, serving_size='100g', **kw)
salmon=mk('Grilled Salmon', allergens=['fish'], allergen_source='verified')
chicken=mk('Chicken Breast', allergens=[], allergen_source='verified')
milk=mk('Whole Milk', allergens=['milk'], allergen_source='verified')
u=CustomUser.objects.create_user(email='pl@x.com',username='pl',password='Xx!23456')
UserFoodPreference.objects.create(user=u, allergies='fish, milk')
p=RuleBasedPlanner(u)
pool={'Lunch':{'protein':[salmon,chicken,milk],'carb':[],'fat':[],'vegetable':[],'fruit':[]}}
out=p._filter_pool_for_allergens(pool)
kept=[f.name for f in out['Lunch']['protein']]
print("  candidate pool before :", ['Grilled Salmon','Chicken Breast','Whole Milk'])
print("  after allergen filter :", kept)
ok = kept==['Chicken Breast']
print(f"  [{'PASS' if ok else 'FAIL'}] unsafe foods never reach the optimiser")
# and with no allergies declared nothing is dropped
u2=CustomUser.objects.create_user(email='pl2@x.com',username='pl2',password='Xx!23456')
p2=RuleBasedPlanner(u2)
pool2={'Lunch':{'protein':[salmon,chicken,milk],'carb':[],'fat':[],'vegetable':[],'fruit':[]}}
kept2=[f.name for f in p2._filter_pool_for_allergens(pool2)['Lunch']['protein']]
print(f"  [{'PASS' if len(kept2)==3 else 'FAIL'}] no allergies declared -> pool untouched ({len(kept2)}/3)")
r.teardown_databases(old)

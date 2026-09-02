import os, sys, django, logging
sys.path.insert(0,'/Users/mac/Desktop/Git/t2/Training_platform')
os.environ['DJANGO_SETTINGS_MODULE']='training_platform.settings_local'
django.setup()
from django.test.utils import get_runner; from django.conf import settings
r=get_runner(settings)(interactive=False); old=r.setup_databases()
logging.disable(logging.CRITICAL)
from users.models import CustomUser
from diet.models import FoodItem, FoodCategory, DietPlan
import diet.tasks as DT
from unittest import mock
cat=FoodCategory.objects.create(name='c')
for n,p_,c_,f_ in [('Chicken Breast',31,0,3.6),('White Rice',2.7,28,0.3),('Olive Oil',0,0,100),('Broccoli',2.8,7,0.4)]:
    FoodItem.objects.create(name=n, category=cat, api_id=n, calories=int(4*p_+4*c_+9*f_),
                            protein=p_, carbs=c_, fat=f_, serving_size='100g')
u=CustomUser.objects.create_user(email='fb@x.com',username='fb',password='Xx!23456')
u.height=180; u.weight=80; u.age=30; u.gender='male'; u.activity_level='moderate'; u.save()
before=DietPlan.objects.count()
from diet.exceptions import DietParsingError
with mock.patch('diet.ai_services.DietGenerator.generate_plan', side_effect=DietParsingError("model returned junk")):
    try:
        DT.generate_ai_diet_plan(u.id, meal_count=3, snack_count=0)
    except Exception as e:
        print("  task re-raised:", type(e).__name__)
after=DietPlan.objects.count()
print(f"  plans before={before} after={after}")
print(f"  [{'PASS' if after>before else 'FAIL'}] user still receives a plan when the AI path fails permanently")
r.teardown_databases(old)

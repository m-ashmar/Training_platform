import sys; sys.path.insert(0,"/Users/mac/Desktop/Git/t2/Training_platform")
import os, django, json
os.environ['DJANGO_SETTINGS_MODULE']='training_platform.settings_local'
django.setup()
from django.test.utils import setup_test_environment
from django.test.runner import DiscoverRunner
setup_test_environment(); r=DiscoverRunner(verbosity=0,interactive=False); old=r.setup_databases()
from django.test import Client
from django.db import connection, reset_queries
from django.conf import settings
from users.models import CustomUser
from routine.models import Exercise, RoutineTemplate
from diet.models import FoodItem, FoodCategory
from subscription.models import SubscriptionPlan
from rest_framework_simplejwt.tokens import RefreshToken
from decimal import Decimal
settings.DEBUG=True
def mk(u,e,t='client'):
    x=CustomUser.objects.create_user(email=e,username=u,password="P@ssw0rd!123",user_type=t); x.is_active=True; x.save(); return x
trA=mk('trA','a@ex.com','trainer'); trB=mk('trB','b@ex.com','trainer'); cli=mk('cli','c@ex.com')
def auth(x):
    c=Client(); c.defaults['HTTP_AUTHORIZATION']=f'Bearer {RefreshToken.for_user(x).access_token}'; return c
A,B,C = auth(trA),auth(trB),auth(cli)
# Food endpoints are gated by HasDietAccess -> the test users need an ACTIVE
# subscription with diet access, otherwise they get 403 and nothing is cacheable.
from subscription.models import Subscription
from django.utils import timezone
from datetime import timedelta
_plan = SubscriptionPlan.objects.create(name='DietPlan',plan_type='premium',description='d',
                                        price=Decimal('9'),duration_days=30,has_diet_access=True)
for _u in (trA,trB,cli):
    Subscription.objects.create(user=_u, plan=_plan, status='active',
                                end_date=timezone.now()+timedelta(days=30))

cat=FoodCategory.objects.create(name='Protein')
FoodItem.objects.create(name='Chicken',category=cat,calories=165,protein=31,carbs=0,fat=3.6,serving_size_grams=100)
SubscriptionPlan.objects.create(name='Pro',plan_type='premium',description='d',price=Decimal('10'),duration_days=30)
Exercise.objects.create(name='GlobalSquat',description='x')
Exercise.objects.create(name='A-PRIVATE-LIFT',description='x',created_by=trA)
RoutineTemplate.objects.create(name='A-TPL',created_by=trA,is_public=False,goal='hypertrophy')
P=F=0
def chk(label, cond):
    global P,F
    if cond: P+=1; print(f"  PASS  {label}")
    else:    F+=1; print(f"  *** FAIL *** {label}")

def qcount(c,url):
    reset_queries(); resp=c.get(url); return len(connection.queries), resp

print("### 1. CACHE HITS (2nd request must cost fewer queries) ###")
for label,c,url in [("public  /diet/api/food/list/",  A,'/api/diet/api/food/list/'),
                    ("public  /subscription/v1/plans/",A,'/api/subscription/v1/plans/'),
                    ("private /routine/exercises/",   A,'/api/routine/exercises/'),
                    ("private /routine/templates/",   A,'/api/routine/templates/')]:
    q1,r1=qcount(c,url); q2,r2=qcount(c,url)
    chk(f"{label}: {q1} -> {q2} queries", r1.status_code==200 and q2<q1)

print("\n### 2. PUBLIC scope is SHARED across users (real hit-rate) ###")
# /subscription/v1/plans/ is genuinely public - the same catalogue for everyone, so one
# user's cached response is a legitimate hit for the next.
qcount(A,'/api/subscription/v1/plans/')
q_b,_=qcount(B,'/api/subscription/v1/plans/')
chk(f"trainerB reuses trainerA's cached PUBLIC plan list ({q_b} queries)", q_b==0)

print("\n### 2b. ENTITLEMENT-GATED routes must NOT be shared across users ###")
# The diet food routes sit behind [IsAuthenticated, HasDietAccess]. They were registered
# with scope="public", whose cache key is shared by every caller, so a subscriber's
# response could be served to a non-subscriber. They must be private-scoped.
from training_platform.cache_config import CACHEABLE_ROUTES
gated = ['/api/diet/api/food/list/', '/api/diet/api/food/categories/', '/api/diet/v1/food/categories/']
for route in gated:
    chk(f"{route} is private-scoped", CACHEABLE_ROUTES[route]['scope'] == 'private')

print("\n### 3. PRIVATE scope is NOT shared (no cross-user leak) ###")
ra=A.get('/api/routine/exercises/').content.decode()
rb=B.get('/api/routine/exercises/').content.decode()
chk("trainerA sees own private exercise", 'A-PRIVATE-LIFT' in ra)
chk("trainerB does NOT see it (no leak via cache)", 'A-PRIVATE-LIFT' not in rb)
rt_a=A.get('/api/routine/templates/').content.decode()
rt_b=B.get('/api/routine/templates/').content.decode()
chk("trainerB does NOT see A's private template", 'A-TPL' in rt_a and 'A-TPL' not in rt_b)

print("\n### 4. INVALIDATION on write (version bump) ###")
FoodItem.objects.create(name='NEW-FOOD',category=cat,calories=1,protein=1,carbs=1,fat=1,serving_size_grams=100)
chk("new food appears immediately (public cache invalidated)", 'NEW-FOOD' in A.get('/api/diet/api/food/list/').content.decode())
Exercise.objects.create(name='NEW-EX',description='x')
chk("new exercise appears immediately (private cache invalidated)", 'NEW-EX' in A.get('/api/routine/exercises/').content.decode())
SubscriptionPlan.objects.create(name='NEW-PLAN',plan_type='basic',description='d',price=Decimal('5'),duration_days=30)
chk("new plan appears immediately", 'NEW-PLAN' in A.get('/api/subscription/v1/plans/').content.decode())

print("\n### 5. Non-registered paths are NOT cached ###")
q1,_=qcount(A,'/api/routine/routines/'); q2,_=qcount(A,'/api/routine/routines/')
chk(f"/routine/routines/ not cached ({q1} then {q2})", q2>0)

print(f"\nRESULT: PASS={P} FAIL={F}")
r.teardown_databases(old)

import os, sys, django, logging, datetime
sys.path.insert(0,'/Users/mac/Desktop/Git/t2/Training_platform')
os.environ['DJANGO_SETTINGS_MODULE']='training_platform.settings_local'
django.setup()
from django.test.utils import get_runner; from django.conf import settings
r=get_runner(settings)(interactive=False); old=r.setup_databases()
logging.disable(logging.ERROR)
from django.test import Client
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken
from users.models import CustomUser
from routine.models import Routine, RoutineProgress
u=CustomUser.objects.create_user(email='st@x.com',username='st',password='Xx!23456'); u.is_active=True; u.save()
c=Client(); c.defaults['HTTP_AUTHORIZATION']=f'Bearer {RefreshToken.for_user(u).access_token}'
today=timezone.localdate()
rt=Routine.objects.create(created_by=u, name='r', start_date=today-datetime.timedelta(days=10), end_date=today+datetime.timedelta(days=10))
rt.assigned_to.add(u)
# consecutive: today, -1, -2  then a GAP at -3, then -4,-5
for d in (0,1,2,4,5):
    RoutineProgress.objects.create(user=u, routine=rt, day=(d%7)+1,
        date=today-datetime.timedelta(days=d), status='completed')
resp=c.get('/api/routine/analytics/streaks/')
print("streaks endpoint:",resp.status_code)
if resp.status_code==200:
    import json; d=resp.json()
    print("  payload:", json.dumps(d)[:300])
    cur=d.get('current_streak', d.get('data',{}).get('current_streak'))
    print(f"\n  completed dates: today,-1,-2  GAP at -3  then -4,-5")
    print(f"  expected current streak = 3")
    print(f"  reported current streak = {cur}   {'OK' if cur==3 else '<<< WRONG'}")
r.teardown_databases(old)

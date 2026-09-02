import os, sys, django, logging, datetime, decimal
sys.path.insert(0,'/Users/mac/Desktop/Git/t2/Training_platform')
os.environ['DJANGO_SETTINGS_MODULE']='training_platform.settings_local'
django.setup()
from django.test.utils import get_runner; from django.conf import settings
settings.DEBUG=True   # required for connection.queries
r=get_runner(settings)(interactive=False); old=r.setup_databases()
logging.disable(logging.ERROR)
from django.db import connection, reset_queries
from django.test import Client
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken
from users.models import CustomUser
from social.models import Post, Comment
from routine.models import Exercise, Routine
def mk(u,e,t='client'):
    x=CustomUser.objects.create_user(email=e,username=u,password='Xx!23456'); x.user_type=t; x.is_active=True; x.save(); return x
alice=mk('a','a@x.com'); trainer=mk('t','t@x.com','trainer')
c=Client(); c.defaults['HTTP_AUTHORIZATION']=f'Bearer {RefreshToken.for_user(alice).access_token}'

def seed(n):
    for i in range(n):
        p=Post.objects.create(author=alice, content=f'p{i}', post_type='text', visibility='public')
        Comment.objects.create(post=p, author=alice, content='c')
    for i in range(n):
        Exercise.objects.create(name=f'ex{i}', created_by=trainer, is_global=True)

def measure(path):
    reset_queries(); resp=c.get(path); return resp.status_code, len(connection.queries)

ENDPOINTS=['/api/social/posts/','/api/social/comments/','/api/routine/exercises/',
           '/api/social/notifications/','/api/routine/routines/','/api/auth/trainers/public/']
seed(5)
first={p:measure(p) for p in ENDPOINTS}
seed(25)   # 6x the rows
second={p:measure(p) for p in ENDPOINTS}
print(f"{'endpoint':40} {'5 rows':>12} {'30 rows':>12}   verdict")
for p in ENDPOINTS:
    s1,q1=first[p]; s2,q2=second[p]
    growth=q2-q1
    verdict = "N+1  <<<" if growth>=5 else ("grows" if growth>1 else "flat")
    print(f"{p:40} {str(s1)+'/'+str(q1)+'q':>12} {str(s2)+'/'+str(q2)+'q':>12}   {verdict}")
r.teardown_databases(old)

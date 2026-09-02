import os, sys, django, json, logging
sys.path.insert(0,'/Users/mac/Desktop/Git/t2/Training_platform')
os.environ['DJANGO_SETTINGS_MODULE']='training_platform.settings_local'
django.setup()
from django.test.utils import get_runner; from django.conf import settings
r=get_runner(settings)(interactive=False); old=r.setup_databases()
logging.disable(logging.ERROR)
from django.test import Client
from rest_framework_simplejwt.tokens import RefreshToken
from users.models import CustomUser
from social.models import Post, Comment, Challenge
def mk(u,e,t='client'):
    x=CustomUser.objects.create_user(email=e,username=u,password='Xx!23456'); x.user_type=t; x.is_active=True; x.save(); return x
def cl(u):
    c=Client(); c.defaults['HTTP_AUTHORIZATION']=f'Bearer {RefreshToken.for_user(u).access_token}'; return c
alice=mk('alice','a@x.com'); bob=mk('bob','b@x.com')
ca, cb = cl(alice), cl(bob)

print("=== A. POST owned by alice ===")
p=Post.objects.create(author=alice, content='alice private thoughts', post_type='text', visibility='public')
print("  bob PATCH :", cb.patch(f'/api/social/posts/{p.id}/', json.dumps({'content':'HACKED'}), content_type='application/json').status_code)
p.refresh_from_db(); print("  content now:", repr(p.content))
print("  bob DELETE:", cb.delete(f'/api/social/posts/{p.id}/').status_code, "| still exists:", Post.objects.filter(id=p.id).exists())

print("\n=== B. COMMENT owned by alice ===")
p2=Post.objects.create(author=alice, content='host', post_type='text', visibility='public')
cm=Comment.objects.create(post=p2, author=alice, content='alice comment')
print("  bob PATCH :", cb.patch(f'/api/social/comments/{cm.id}/', json.dumps({'content':'HACKED'}), content_type='application/json').status_code)
cm.refresh_from_db(); print("  content now:", repr(cm.content))
print("  bob DELETE:", cb.delete(f'/api/social/comments/{cm.id}/').status_code, "| still exists:", Comment.objects.filter(id=cm.id).exists())

print("\n=== C. CHALLENGE created by alice ===")
import datetime
ch=Challenge.objects.create(creator=alice, title='alice challenge', description='d',
    challenge_type=Challenge._meta.get_field('challenge_type').choices[0][0],
    start_date=datetime.date.today(), end_date=datetime.date.today()+datetime.timedelta(days=7))
print("  bob PATCH :", cb.patch(f'/api/social/challenges/{ch.id}/', json.dumps({'title':'HACKED'}), content_type='application/json').status_code)
ch.refresh_from_db(); print("  title now:", repr(ch.title))
print("  bob DELETE:", cb.delete(f'/api/social/challenges/{ch.id}/').status_code, "| still exists:", Challenge.objects.filter(id=ch.id).exists())

print("\n=== D. PRIVATE post visible to bob via API? ===")
pv=Post.objects.create(author=alice, content='SECRET', post_type='text', visibility='private')
rr=cb.get(f'/api/social/posts/{pv.id}/'); print("  bob GET private post:", rr.status_code)
rl=cb.get('/api/social/posts/'); body=rl.content.decode()
print("  'SECRET' leaks in bob's feed list:", 'SECRET' in body)
r.teardown_databases(old)

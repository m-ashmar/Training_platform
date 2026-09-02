import os, sys, django, logging, json
sys.path.insert(0,'/Users/mac/Desktop/Git/t2/Training_platform')
os.environ['DJANGO_SETTINGS_MODULE']='training_platform.settings_local'
django.setup()
from django.test.utils import get_runner; from django.conf import settings
r=get_runner(settings)(interactive=False); old=r.setup_databases()
logging.disable(logging.CRITICAL)
from celery import current_app
# simulate production: broker configured but nothing listening (no worker, wrong host)
current_app.conf.task_always_eager=False
current_app.conf.broker_url='redis://127.0.0.1:6399/0'
current_app.conf.broker_transport_options={'max_retries':0}
current_app.conf.broker_connection_retry_on_startup=False
current_app.conf.broker_connection_max_retries=0
from django.test import Client
from rest_framework_simplejwt.tokens import RefreshToken
from users.models import CustomUser
from social.models import Post
u=CustomUser.objects.create_user(email='bk@x.com',username='bk',password='Xx!23456'); u.is_active=True; u.save()
c=Client(); c.defaults['HTTP_AUTHORIZATION']=f'Bearer {RefreshToken.for_user(u).access_token}'
print("### user creates a post while the Celery broker is unreachable ###")
resp=c.post('/api/social/posts/', json.dumps({'content':'hello','post_type':'text','visibility':'public'}),
            content_type='application/json')
print(f"   POST /api/social/posts/ -> {resp.status_code}")
print(f"   post actually stored: {Post.objects.count()==1}")
if resp.status_code>=500:
    print("   *** a broker outage takes down a core user action ***")
elif Post.objects.count()==1:
    print("   post saved; fan-out silently lost (no worker to run it)")
r.teardown_databases(old)

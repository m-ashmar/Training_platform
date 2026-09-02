import os, io, sys, django
sys.path.insert(0,'/Users/mac/Desktop/Git/t2/Training_platform')
os.environ['DJANGO_SETTINGS_MODULE']='training_platform.settings_local'
django.setup()
from django.test.utils import get_runner; from django.conf import settings
r=get_runner(settings)(interactive=False); old=r.setup_databases()
from django.test import Client
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework_simplejwt.tokens import RefreshToken
from PIL import Image
from users.models import CustomUser
from social.models import Post
def png():
    b=io.BytesIO(); Image.new('RGB',(30,30),(9,9,9)).save(b,'PNG'); b.seek(0); return b.read()
alice=CustomUser.objects.create_user(email='a@t.com',username='alice',password='Xx!23456'); alice.is_active=True; alice.save()
ca=Client(); ca.defaults['HTTP_AUTHORIZATION']=f'Bearer {RefreshToken.for_user(alice).access_token}'
resp=ca.post('/api/social/posts/',{'content':'secret','post_type':'workout','visibility':'private',
    'image':SimpleUploadedFile('vacation.png',png(),content_type='image/png')})
p=Post.objects.first()
print("post created:",resp.status_code,"visibility:",p.visibility,"image:",p.image.name)

# 1. Can a stranger read the POST through the API?
bob=CustomUser.objects.create_user(email='b@t.com',username='bob',password='Xx!23456'); bob.is_active=True; bob.save()
cb=Client(); cb.defaults['HTTP_AUTHORIZATION']=f'Bearer {RefreshToken.for_user(bob).access_token}'
rd=cb.get(f'/api/social/posts/{p.id}/')
print(f"bob GET private post via API: {rd.status_code}  (expect 403/404)")

# 2. Can an ANONYMOUS client read the image file directly?
anon=Client()
url=p.image.url
ri=anon.get(url)
# Media is served WITHOUT an authorization check - that is deliberate, because the
# Flutter client loads image URLs directly and does not attach a JWT to them. The
# security property is therefore the unguessable path (a capability URL), not access
# control: anyone holding the link can fetch it, nobody can enumerate it.
# Replacing this with true per-request authz is an open decision (see BUG_REGISTRY).
predictable = p.image.name == 'posts/vacation.png'
print(f"anonymous GET {url}: {ri.status_code}")
print(f"  [{'FAIL' if predictable else 'PASS'}] stored path is NOT derived from the uploader's filename")
print(f"  [{'PASS' if len(p.image.name.split('/')[-1].split('.')[0])>=32 else 'FAIL'}] path carries >=128 bits of entropy")
print(f"  [{'PASS' if ri.headers.get('X-Content-Type-Options')=='nosniff' else 'FAIL'}] nosniff header present")
if ri.status_code==200:
    print("  headers:", {k:v for k,v in ri.headers.items() if k.lower() in
          ('content-type','x-content-type-options','content-disposition','cache-control')})
r.teardown_databases(old)

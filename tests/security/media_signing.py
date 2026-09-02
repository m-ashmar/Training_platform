import os, sys, django, logging, io, json, time
sys.path.insert(0,'/Users/mac/Desktop/Git/t2/Training_platform')
os.environ['DJANGO_SETTINGS_MODULE']='training_platform.settings_local'
django.setup()
from django.test.utils import get_runner; from django.conf import settings
r=get_runner(settings)(interactive=False); old=r.setup_databases()
logging.disable(logging.ERROR)
from django.test import Client
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework_simplejwt.tokens import RefreshToken
from PIL import Image
from users.models import CustomUser
from social.models import Post
def png():
    b=io.BytesIO(); Image.new('RGB',(30,30),(4,4,4)).save(b,'PNG'); b.seek(0); return b.read()
u=CustomUser.objects.create_user(email='ms@x.com',username='ms',password='Xx!23456'); u.is_active=True; u.save()
c=Client(); c.defaults['HTTP_AUTHORIZATION']=f'Bearer {RefreshToken.for_user(u).access_token}'
c.post('/api/social/posts/',{'content':'x','post_type':'text','visibility':'private',
       'image':SimpleUploadedFile('v.png',png(),content_type='image/png')})
p=Post.objects.first()
url=p.image.url
print("MEDIA_URL_SIGNING:", settings.MEDIA_URL_SIGNING, "| TTL:", settings.MEDIA_URL_TTL)
print("signed url:", url[:78], "...")
res=[]
res.append(("url carries a signature", '?s=' in url))
anon=Client()
res.append(("signed url loads", anon.get(url).status_code==200))
bare=url.split('?')[0]
res.append(("UNSIGNED url refused (404)", anon.get(bare).status_code==404))
res.append(("tampered signature refused", anon.get(bare+'?s=deadbeef:xxxx').status_code==404))
# URL must be byte-stable so the client can cache the image (signing used to embed
# now(), so every serialization produced a different URL and forced a re-download).
p.refresh_from_db()
res.append(("url is stable across accesses", p.image.url == url))
# Expiry: signatures are bucketed, so simulate the clock moving two windows on.
import training_platform.media_storage as MS
real_bucket = MS._bucket
MS._bucket = lambda offset=0: real_bucket(offset) + 2
res.append(("signature from an old window refused", anon.get(url).status_code==404))
MS._bucket = real_bucket
res.append(("valid again in the current window", anon.get(url).status_code==200))
# External storage must not sign — that provider issues its own URLs.
settings.USE_EXTERNAL_MEDIA_STORAGE = True
res.append(("external storage yields an unsigned url", '?s=' not in p.image.storage.url(p.image.name)))
settings.USE_EXTERNAL_MEDIA_STORAGE = False
for k,v in res: print(f"  [{'PASS' if v else 'FAIL'}] {k}")
print(f"\n{sum(1 for _,v in res if v)}/{len(res)} PASS")
r.teardown_databases(old)

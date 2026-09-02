import sys; sys.path.insert(0,"/Users/mac/Desktop/Git/t2/Training_platform")
import os, django, tempfile, shutil, io
os.environ['DJANGO_SETTINGS_MODULE']='training_platform.settings_local'
django.setup()
from django.test.utils import setup_test_environment
from django.test.runner import DiscoverRunner
setup_test_environment(); r=DiscoverRunner(verbosity=0,interactive=False); old=r.setup_databases()
from django.test import Client
from django.core.files.uploadedfile import SimpleUploadedFile
from django.conf import settings
from users.models import CustomUser
from routine.models import Exercise
from rest_framework_simplejwt.tokens import RefreshToken
from PIL import Image
import logging; logging.disable(logging.CRITICAL)
MR=tempfile.mkdtemp(prefix='p8sec_'); settings.MEDIA_ROOT=MR
P=F=0
def chk(label, cond):
    global P,F
    if cond: P+=1; print(f"  PASS  {label}")
    else:    F+=1; print(f"  *** FAIL *** {label}")

def real_image(fmt='PNG', size=(80,80), comment=None):
    buf=io.BytesIO(); img=Image.new('RGB',size,(20,120,200))
    kw={}
    if comment and fmt=='JPEG': kw['comment']=comment
    img.save(buf,format=fmt,**kw); return buf.getvalue()

cli=CustomUser.objects.create_user(email='c@ex.com',username='c',password="P@ssw0rd!123",user_type='client'); cli.is_active=True; cli.save()
tr=CustomUser.objects.create_user(email='t@ex.com',username='t',password="P@ssw0rd!123",user_type='trainer'); tr.is_active=True; tr.save()
def auth(x):
    c=Client(); c.defaults['HTTP_AUTHORIZATION']=f'Bearer {RefreshToken.for_user(x).access_token}'; return c
C,T=auth(cli),auth(tr)
ex=Exercise.objects.create(name='Squat',description='x',created_by=tr)
PP='/api/auth/user/profile-picture/'
EI=f'/api/routine/exercises/{ex.id}/image/'

print("### LEGITIMATE uploads MUST still work ###")
for fmt in ['PNG','JPEG','GIF','WEBP']:
    resp=C.post(PP, {'profile_picture': SimpleUploadedFile(f"real.{fmt.lower()}", real_image(fmt), content_type=f"image/{fmt.lower()}")})
    cli.refresh_from_db()
    chk(f"real {fmt} profile picture accepted (stored {cli.profile_picture.name.split('.')[-1] if cli.profile_picture else '-'})", resp.status_code==200)
resp=T.post(EI, {'image': SimpleUploadedFile("real.png", real_image('PNG'), content_type="image/png")})
ex.refresh_from_db()
chk(f"real PNG exercise image accepted (stored .{ex.image.name.split('.')[-1] if ex.image else '-'})", resp.status_code==200)

print("\n### MALICIOUS uploads MUST be blocked ###")
for label,fn,content,ct in [
  ("PHP shell as image/jpeg","shell.php", b"<?php system($_GET['c']); ?>", "image/jpeg"),
  ("HTML+JS as image/png",   "x.html",    b"<script>alert(document.cookie)</script>", "image/png"),
  ("SVG+script as image/png","x.svg",     b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', "image/png"),
  ("polyglot JPEG+PHP",      "poly.jpg",  b'\xff\xd8\xff\xe0'+b'<?php system($_GET["c"]); ?>', "image/jpeg"),
]:
    resp=C.post(PP, {'profile_picture': SimpleUploadedFile(fn, content, content_type=ct)})
    chk(f"{label} blocked ({resp.status_code})", resp.status_code==400)

print("\n### Renaming a payload to .png must NOT help (content is what counts) ###")
resp=C.post(PP, {'profile_picture': SimpleUploadedFile("innocent.png", b"<?php system($_GET['c']); ?>", content_type="image/png")})
chk(f"php payload named .png blocked ({resp.status_code})", resp.status_code==400)

print("\n### Decompression bomb blocked ###")
buf=io.BytesIO(); Image.new('RGB',(12000,12000),(255,0,0)).save(buf,format='PNG',optimize=True)
resp=T.post(EI, {'image': SimpleUploadedFile("bomb.png", buf.getvalue(), content_type="image/png")})
chk(f"12000x12000 bomb blocked ({resp.status_code})", resp.status_code==400)

print("\n### EXIF / metadata stripped by re-encode ###")
raw=real_image('JPEG', comment=b'SECRET-GPS-METADATA')
chk("source file really contains the metadata", b'SECRET-GPS-METADATA' in raw)
resp=T.post(EI, {'image': SimpleUploadedFile("meta.jpg", raw, content_type="image/jpeg")})
ex.refresh_from_db()
stored=open(os.path.join(MR, ex.image.name),'rb').read() if ex.image else b''
chk(f"metadata stripped from stored file ({resp.status_code})", resp.status_code==200 and b'SECRET-GPS-METADATA' not in stored)

print("\n### Stored extension comes from DETECTED format, not filename ###")
resp=C.post(PP, {'profile_picture': SimpleUploadedFile("lies.jpg", real_image('PNG'), content_type="image/jpeg")})
cli.refresh_from_db()
chk(f"PNG uploaded as 'lies.jpg' stored as .png ({cli.profile_picture.name.split('.')[-1]})",
    resp.status_code==200 and cli.profile_picture.name.endswith('.png'))

print(f"\nRESULT: PASS={P} FAIL={F}")
shutil.rmtree(MR, ignore_errors=True)
r.teardown_databases(old)

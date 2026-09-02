import os, io, sys, django
sys.path.insert(0, '/Users/mac/Desktop/Git/t2/Training_platform')
os.environ.setdefault('DJANGO_SETTINGS_MODULE','training_platform.settings_local')
django.setup()
from django.test.utils import get_runner; from django.conf import settings
r=get_runner(settings)(interactive=False); old=r.setup_databases()
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import transaction
from PIL import Image
from users.models import CustomUser

def png(sz=(40,40)):
    b=io.BytesIO(); Image.new('RGB',sz,(1,2,3)).save(b,'PNG'); b.seek(0); return b.read()

u=CustomUser.objects.create_user(email='fl@t.com',username='fl',password='Xx!23456')
res=[]

# 1. REPLACEMENT: does the old file get removed when a new one is uploaded?
u.profile_picture.save('a.png', ContentFile(png()), save=True)
first = u.profile_picture.name
u.profile_picture.save('b.png', ContentFile(png()), save=True)
second = u.profile_picture.name
res.append(("replaced file removed", first != second and not default_storage.exists(first)))
res.append(("new file present", default_storage.exists(second)))

# 2. ROLLBACK: a rolled-back save must NOT destroy the live file
current = u.profile_picture.name
try:
    with transaction.atomic():
        u.profile_picture.save('c.png', ContentFile(png()), save=True)
        raise RuntimeError("forced rollback")
except RuntimeError:
    pass
u.refresh_from_db()
res.append(("rollback kept live file", default_storage.exists(u.profile_picture.name)))
res.append(("rollback restored old name", u.profile_picture.name == current))

# 3. SHARED file guard: two rows on one path -> deleting one must not orphan the other
# NOTE: CustomUser is no longer hard-deletable (Wallet.owner/Subscription.user are
# PROTECT, so a delete can never erase the ledger). Wallets are created lazily, so
# these two users have none and remain deletable - which is what exercises the receiver.
u2=CustomUser.objects.create_user(email='fl2@t.com',username='fl2',password='Xx!23456')
u3=CustomUser.objects.create_user(email='fl3@t.com',username='fl3',password='Xx!23456')
shared='profile_pics/shared_test.png'
default_storage.save(shared, ContentFile(png()))
CustomUser.objects.filter(pk__in=[u2.pk,u3.pk]).update(profile_picture=shared)
# Users are PROTECTed from deletion now, so exercise the shared-path guard through the
# replace path instead: repointing one holder must not remove a file the other still uses.
x=CustomUser.objects.get(pk=u2.pk); x.profile_picture.save('other.png', ContentFile(png()), save=True)
res.append(("shared file survives sibling repoint", default_storage.exists(shared)))
y=CustomUser.objects.get(pk=u3.pk); y.profile_picture.save('other2.png', ContentFile(png()), save=True)
res.append(("last holder repoint removes file", not default_storage.exists(shared)))

# hard delete is refused; retire() is the supported path and must clear the picture
from django.db.models.deletion import ProtectedError
u5=CustomUser.objects.create_user(email='fl5@t.com',username='fl5',password='Xx!23456')
u5.profile_picture.save('e.png', ContentFile(png()), save=True); pic=u5.profile_picture.name
try:
    u5.delete(); res.append(("user hard-delete refused", False))
except ProtectedError:
    res.append(("user hard-delete refused (ledger safe)", True))
u5.retire(reason='test')
u5.refresh_from_db()
res.append(("retire() deactivates", not u5.is_active))
res.append(("retire() anonymises email", u5.email.startswith('retired+')))
res.append(("retire() removes profile picture file", not default_storage.exists(pic)))

# 4. queryset.delete() (bulk) also cleans up
# bulk delete on a model that is still deletable (Exercise carries an image too)
from routine.models import Exercise
tr=CustomUser.objects.create_user(email='fl4@t.com',username='fl4',password='Xx!23456')
ex=Exercise.objects.create(name='bulkex', created_by=tr)
ex.image.save('d.png', ContentFile(png()), save=True)
n=ex.image.name
Exercise.objects.filter(pk=ex.pk).delete()
res.append(("bulk queryset.delete cleans file", not default_storage.exists(n)))

# 5. saving a model with NO file change must not fire an extra delete
u.first_name='x'; u.save()
u.refresh_from_db()
res.append(("no-op save keeps file", default_storage.exists(u.profile_picture.name)))

for k,v in res: print(f"  [{'PASS' if v else 'FAIL'}] {k}")
print(f"\n{sum(1 for _,v in res if v)}/{len(res)} PASS")
r.teardown_databases(old)

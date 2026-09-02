import os, sys, django, decimal, threading, logging
sys.path.insert(0,'/Users/mac/Desktop/Git/t2/Training_platform')
os.environ['DJANGO_SETTINGS_MODULE']='training_platform.settings_local'
django.setup()
from django.test.utils import get_runner; from django.conf import settings
r=get_runner(settings)(interactive=False); old=r.setup_databases()
logging.disable(logging.ERROR)
from django import db
from users.models import CustomUser
from wallet.models import Wallet, move_funds_atomic, Transaction
D=decimal.Decimal
a=CustomUser.objects.create_user(email='ca@x.com',username='ca',password='Xx!23456')
b=CustomUser.objects.create_user(email='cb@x.com',username='cb',password='Xx!23456')
wa,_=Wallet.objects.get_or_create(owner=a); wb,_=Wallet.objects.get_or_create(owner=b)
wa.balance=D('100.00'); wa.save(); wb.balance=D('0.00'); wb.save()

print("### A. double-spend: 10 threads each moving 100.00 from a 100.00 wallet ###")
errs=[]; oks=[]
def worker():
    try:
        move_funds_atomic(wa, wb, D('100.00'), actor_id=a.id); oks.append(1)
    except Exception as e: errs.append(type(e).__name__+':'+str(e)[:40])
    finally: db.connections.close_all()
ts=[threading.Thread(target=worker) for _ in range(10)]
[t.start() for t in ts]; [t.join() for t in ts]
wa.refresh_from_db(); wb.refresh_from_db()
print(f"  succeeded={len(oks)} failed={len(errs)}  src={wa.balance} dst={wb.balance}")
print(f"  conserved (src+dst==100): {wa.balance+wb.balance==D('100.00')}  double-spend: {len(oks)>1}")

print("\n### B. reverse-direction concurrent transfers (lock-order deadlock?) ###")
wa.balance=D('500.00'); wa.save(); wb.balance=D('500.00'); wb.save()
res=[]
def fwd():
    try:
        for _ in range(12): move_funds_atomic(wa, wb, D('1.00'), actor_id=a.id)
        res.append('fwd ok')
    except Exception as e: res.append('fwd '+type(e).__name__+': '+str(e)[:60])
    finally: db.connections.close_all()
def rev():
    try:
        for _ in range(12): move_funds_atomic(wb, wa, D('1.00'), actor_id=b.id)
        res.append('rev ok')
    except Exception as e: res.append('rev '+type(e).__name__+': '+str(e)[:60])
    finally: db.connections.close_all()
t1=threading.Thread(target=fwd); t2=threading.Thread(target=rev)
t1.start(); t2.start(); t1.join(); t2.join()
for x in res: print("  ",x)
wa.refresh_from_db(); wb.refresh_from_db()
print(f"  totals conserved: {wa.balance+wb.balance==D('1000.00')} ({wa.balance}+{wb.balance})")
r.teardown_databases(old)

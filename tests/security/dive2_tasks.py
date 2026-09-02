import os, sys, django, logging, traceback
sys.path.insert(0,'/Users/mac/Desktop/Git/t2/Training_platform')
os.environ['DJANGO_SETTINGS_MODULE']='training_platform.settings_local'
django.setup()
from django.test.utils import get_runner; from django.conf import settings
r=get_runner(settings)(interactive=False); old=r.setup_databases()
logging.disable(logging.CRITICAL)
from django.utils import timezone
from users.models import CustomUser
from social.models import Post
u=CustomUser.objects.create_user(email='tk@x.com',username='tk',password='Xx!23456'); u.is_active=True; u.save()
p=Post.objects.create(author=u, content='c', post_type='text', visibility='public')

import routine.tasks as RT, diet.tasks as DT, social.tasks as ST, ai_assistant.tasks as AT, notifications.tasks as NT
GHOST = 999999   # a user id that does not exist — the realistic "user deleted / bad payload" case

CASES=[
 ("routine.send_async_notification  (valid)", lambda: RT.send_async_notification(u.id,'info','hello')),
 ("routine.send_async_notification  (ghost user)", lambda: RT.send_async_notification(GHOST,'info','hello')),
 ("social.fan_out_post_root         (valid)", lambda: ST.fan_out_post_root(u.id,p.id,timezone.now().isoformat())),
 ("social.fan_out_post_root         (ghost post)", lambda: ST.fan_out_post_root(u.id,GHOST,timezone.now().isoformat())),
 ("social.fan_out_batch             (ghost users)", lambda: ST.fan_out_batch([GHOST],p.id,timezone.now().isoformat())),
 ("social.send_firebase_notification(ghost user)", lambda: ST.send_firebase_notification(GHOST,'t','b')),
 ("ai.close_idle_sessions", lambda: AT.close_idle_sessions()),
 ("ai.compute_all_user_insights", lambda: AT.compute_all_user_insights()),
 ("ai.check_daily_cost", lambda: AT.check_daily_cost()),
 ("diet.generate_daily_advice       (no user)", lambda: DT.generate_daily_advice()),
 ("diet.generate_daily_advice       (ghost user)", lambda: DT.generate_daily_advice(GHOST)),
 ("notifications.process_event_task (bad path)", lambda: NT.process_event_task('nope.NoSuchEvent',{})),
 ("notifications.process_event_task (empty)", lambda: NT.process_event_task('',{})),
]
print(f"{'task':50} outcome")
for label, fn in CASES:
    try:
        fn(); print(f"{label:50} ok")
    except Exception as e:
        tb=traceback.format_exc().strip().split('\n')
        proj=[l.strip() for l in tb if 'Training_platform/' in l and '.venv' not in l]
        loc=proj[-1] if proj else ''
        print(f"{label:50} *** {type(e).__name__}: {str(e)[:60]}")
        if loc: print(f"{'':50}     {loc[:110]}")
r.teardown_databases(old)

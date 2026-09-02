import os, sys, django, datetime
sys.path.insert(0,'/Users/mac/Desktop/Git/t2/Training_platform')
os.environ['DJANGO_SETTINGS_MODULE']='training_platform.settings_local'
os.environ['TZ']='UTC'
import time
try: time.tzset()
except Exception: pass
django.setup()
from django.utils import timezone
from django.conf import settings
import zoneinfo
DAM=zoneinfo.ZoneInfo(settings.TIME_ZONE)
print("settings.TIME_ZONE =",settings.TIME_ZONE,"| USE_TZ =",settings.USE_TZ,"| container TZ = UTC (Fly default)")
print()
for label,utc in [("a user logging a 01:30 Damascus workout", datetime.datetime(2026,3,10,22,30,tzinfo=datetime.timezone.utc)),
                  ("same user at 14:00 Damascus",             datetime.datetime(2026,3,10,11,0, tzinfo=datetime.timezone.utc))]:
    local = utc.astimezone(DAM)
    print(f"{label}:")
    print(f"   real Damascus wall clock : {local:%Y-%m-%d %H:%M}   -> business date {local.date()}")
    print(f"   timezone.now().date()    : {utc.date()}          {'<<< WRONG DAY' if utc.date()!=local.date() else ''}")
    print(f"   date.today() (UTC box)   : {utc.date()}          {'<<< WRONG DAY' if utc.date()!=local.date() else ''}")
    print(f"   timezone.localdate()     : {timezone.localdate(utc)}          correct")
    print()
# how many hours a day are wrong
print("Damascus is UTC+3 -> every day from 00:00 to 03:00 local, both forms report YESTERDAY.")
print("That is 3 of every 24 hours = 12.5% of the clock, every single day.")

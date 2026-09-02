import os, sys, django, logging, json
sys.path.insert(0,'/Users/mac/Desktop/Git/t2/Training_platform')
os.environ['DJANGO_SETTINGS_MODULE']='training_platform.settings_local'
django.setup()
from django.test.utils import get_runner; from django.conf import settings
r=get_runner(settings)(interactive=False); old=r.setup_databases()
logging.disable(logging.CRITICAL)
from users.models import CustomUser
from ai_assistant.tool_registry import ToolRegistry, _TOOL_FUNCTIONS
alice=CustomUser.objects.create_user(email='ta@x.com',username='alice',password='Xx!23456'); alice.is_active=True; alice.save()
bob=CustomUser.objects.create_user(email='tb@x.com',username='bob',password='Xx!23456'); bob.is_active=True; bob.save()
alice.height=180; alice.weight=99; alice.specific_injury='SECRET INJURY'; alice.save()

reg=ToolRegistry(bob)   # registry bound to BOB
print("registered tools:", sorted(_TOOL_FUNCTIONS))
print(f"\nBob's registry; attacker-controlled arguments try to reach Alice (id={alice.id}):\n")
ATTACKS=[
  ("user_id override",      {"user_id": alice.id}),
  ("user override",         {"user": alice.id}),
  ("username override",     {"username": "alice"}),
  ("id override",           {"id": alice.id}),
  ("target_user",           {"target_user": alice.id}),
  ("nested",                {"filters": {"user_id": alice.id}}),
]
leaked=0
for tool in sorted(_TOOL_FUNCTIONS):
    for label, args in ATTACKS:
        try:
            out = reg.execute(tool, dict(args))
        except Exception as e:
            out = {"error": f"raised {type(e).__name__}"}
        blob = json.dumps(out, default=str)
        if 'SECRET INJURY' in blob or '"99"' in blob or 'alice' in blob.lower():
            print(f"  *** LEAK  {tool} via {label}: {blob[:110]}")
            leaked += 1
print(f"\ncross-user leaks via tool arguments: {leaked}")

print("\n--- does a tool accept arbitrary kwargs silently? ---")
out = reg.execute('get_user_profile', {"user_id": alice.id, "bogus": 1}) if 'get_user_profile' in _TOOL_FUNCTIONS else None
print("  get_user_profile with junk kwargs ->", json.dumps(out, default=str)[:150] if out else "(tool absent)")
print("\n--- unknown tool name ---")
print("  ", reg.execute('__import__', {}))
r.teardown_databases(old)

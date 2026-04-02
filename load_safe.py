import os
import django
from django.core.management import call_command
from django.db.models.signals import post_save

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'training_platform.settings')
django.setup()

from django.dispatch import Signal
from subscription.models import Payment
from django.apps import apps
from django.contrib.contenttypes.management import create_contenttypes
from django.contrib.auth.management import create_permissions

def load_safe():
    print("Monkey-patching Signal.send to disable ALL signals...")
    original_send = Signal.send
    def noop_send(self, sender, **named):
        return []
    Signal.send = noop_send

    # Patch Payment.save to bypass validation
    print("Patching Payment.save...")
    original_save = Payment.save
    def unsafe_save(self, *args, **kwargs):
        # Bypass full_clean
        super(Payment, self).save(*args, **kwargs)
    Payment.save = unsafe_save
    
    print("Signals disabled. Flushing database...")
    call_command('flush', '--no-input')
    
    print("Regenerating ContentTypes and Permissions...")
    for app_config in apps.get_app_configs():
        create_contenttypes(app_config, verbosity=0)
        create_permissions(app_config, verbosity=0)
    
    print("Loading data...")
    try:
        call_command('loaddata', 'datadump_clean.json')
        print("Data loaded successfully!")
    except Exception as e:
        print(f"FAILED to load data: {e}")
    finally:
        print("Restoring signals and methods...")
        Signal.send = original_send
        Payment.save = original_save

if __name__ == '__main__':
    load_safe()

import os
import django

# Setup Django Environment Native Context
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "training_platform.settings_local")
django.setup()

from users.models import CustomUser
from wallet.models import Wallet, AgentProfile
from django.db import transaction

print("Starting Escrow Account Configuration...")

with transaction.atomic():
    # 1. Check if escrow already exists
    escrow_user = CustomUser.objects.filter(username="platform_escrow").first()
    
    if not escrow_user:
        # Create the system holding user securely mapped as an Agent
        escrow_user = CustomUser.objects.create_user(
            username="platform_escrow",
            email="escrow@platform.local",
            password="ComplexPlaceholderPassword123!@",
            user_type="agent",
            is_active=True
        )
        print(f"[*] Created User object representing Escrow (ID: {escrow_user.id})")
    
    # 2. Attach the specific Agent identity and Wallet constraints
    agent_profile, created_ap = AgentProfile.objects.get_or_create(
        user=escrow_user,
        defaults={
            "wallet_type": "prepaid",
            "status": "active"
        }
    )
    if created_ap:
        print("[*] Generated native AgentProfile layer")
    
    wallet, created_w = Wallet.objects.get_or_create(
        owner=escrow_user,
        defaults={
            "owner_type": "agent",
            "balance": 0.00,
            "currency": "USD"
        }
    )
    if created_w:
        print(f"[*] Provisioned Financial Ledger (Wallet ID: {wallet.id})")

    print(f"\n✅ Escrow System Account built successfully! Configuration Target Wallet ID: {wallet.id}")


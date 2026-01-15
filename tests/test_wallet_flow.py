import time
import pytest
from django.urls import reverse
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from wallet.models import Wallet, AgentProfile, AgentAPIKey
from wallet.security import compute_hmac_signature


pytestmark = pytest.mark.django_db


def create_user(email, username, user_type, password="pass12345"):
    User = get_user_model()
    return User.objects.create_user(email=email, username=username, password=password, user_type=user_type)


def test_wallet_end_to_end_flow():
    client_user = create_user("client@example.com", "client", "client")
    trainer_user = create_user("trainer@example.com", "trainer", "trainer")
    agent_user = create_user("agent@example.com", "agent", "agent")

    # Prepare agent
    agent_profile = AgentProfile.objects.get(user=agent_user)
    agent_profile.daily_limit = 1000
    agent_profile.monthly_limit = 10000
    agent_profile.ip_allowlist = ["127.0.0.1"]
    agent_profile.save()
    api_key = AgentAPIKey.objects.create(agent=agent_profile, key_id="testkey", hashed_key="secret")

    # Client login
    api = APIClient()
    api.force_authenticate(user=client_user)

    # Ensure wallets exist
    Wallet.objects.get_or_create(owner=client_user, defaults={"owner_type": "client"})
    Wallet.objects.get_or_create(owner=trainer_user, defaults={"owner_type": "trainer"})

    # Agent top-up
    agent_api = APIClient()
    agent_api.force_authenticate(user=agent_user)
    ts = int(time.time())
    idem = "idem-1"
    message = f"{client_user.email}|100.00|{ts}|{idem}"
    sig = compute_hmac_signature("secret", message)
    agent_api.credentials(HTTP_X_AGENT_AUTH=f"AgentAuth key_id={api_key.key_id},signature={sig},timestamp={ts}")

    r = agent_api.post(reverse("wallet:agent-topup"), {
        "client_identifier": client_user.email,
        "amount": "100.00",
        "idempotency_key": idem,
        "timestamp": ts,
        "signature": sig,
    }, format="json")
    assert r.status_code == 200, r.content

    # Client transfers to trainer
    r2 = api.post(reverse("wallet:client-transfer"), {
        "trainer_id": trainer_user.id,
        "amount": "30.00",
        "idempotency_key": "idem-2",
    }, format="json")
    assert r2.status_code == 200, r2.content

    # Admin reversal
    admin_user = create_user("admin@example.com", "admin", "admin")
    admin_user.is_superuser = True
    admin_user.is_staff = True
    admin_user.save()

    admin_api = APIClient()
    admin_api.force_authenticate(user=admin_user)
    ref = r2.json()["reference_id"]
    r3 = admin_api.post(reverse("wallet:admin-reversal"), {
        "reference_id": ref,
        "reason": "test",
        "idempotency_key": "idem-3",
    }, format="json")
    assert r3.status_code == 200, r3.content



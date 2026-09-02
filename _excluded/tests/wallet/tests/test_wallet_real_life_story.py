import time
from django.test import TestCase
from django.urls import reverse
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from wallet.models import Wallet, AgentProfile, AgentAPIKey
from wallet.security import compute_hmac_signature


def create_user(email, username, user_type, password="pass12345"):
    User = get_user_model()
    return User.objects.create_user(
        email=email,
        username=username,
        password=password,
        user_type=user_type,
        phone_number="0000000000",
    )


class WalletRealLifeStoryTest(TestCase):
    def setUp(self):
        self.client_user = create_user("client2@example.com", "client2", "client")
        self.trainer_user = create_user("trainer2@example.com", "trainer2", "trainer")
        self.trainer_user.trainer_hourly_rate = 25
        self.trainer_user.save()
        self.agent_user = create_user("agent2@example.com", "agent2", "agent")

        # Setup agent
        ap = AgentProfile.objects.get(user=self.agent_user)
        ap.daily_limit = 10000
        ap.monthly_limit = 100000
        ap.ip_allowlist = ["127.0.0.1"]
        ap.save()
        self.api_key = AgentAPIKey.objects.create(agent=ap, key_id="testkey2", hashed_key="secret2")

        # Ensure wallets exist
        Wallet.objects.get_or_create(owner=self.client_user, defaults={"owner_type": "client"})
        Wallet.objects.get_or_create(owner=self.trainer_user, defaults={"owner_type": "trainer"})

    def test_real_life_flow(self):
        # 1) Agent tops up client wallet by 100
        agent_api = APIClient()
        agent_api.force_authenticate(user=self.agent_user)
        ts = int(time.time())
        idem = "idem-rl-1"
        msg = f"{self.client_user.email}|100.00|{ts}|{idem}"
        sig = compute_hmac_signature("secret2", msg)
        agent_api.credentials(HTTP_X_AGENT_AUTH=f"AgentAuth key_id={self.api_key.key_id},signature={sig},timestamp={ts}")
        r1 = agent_api.post(reverse("wallet:agent-topup"), {
            "client_identifier": self.client_user.email,
            "amount": "100.00",
            "idempotency_key": idem,
            "timestamp": ts,
            "signature": sig,
        }, format="json")
        self.assertEqual(r1.status_code, 200, r1.content)

        # 2) Client requests trainer (ensure balance check enforced)
        api = APIClient()
        api.force_authenticate(user=self.client_user)
        r2 = api.post(reverse("users:client_request_trainer"), {"trainer_id": self.trainer_user.id}, format="json")
        self.assertEqual(r2.status_code, 200, r2.content)

        # 3) Trainer approves request -> funds held in escrow
        trainer_api = APIClient()
        trainer_api.force_authenticate(user=self.trainer_user)
        # Find request id
        rpend = trainer_api.get(reverse("users:trainer_pending_requests"))
        self.assertEqual(rpend.status_code, 200, rpend.content)
        req_id = rpend.json()["pending_requests"][0]["request_id"]
        r3 = trainer_api.post(reverse("users:trainer_respond_to_request"), {"request_id": req_id, "action": "approve"}, format="json")
        self.assertEqual(r3.status_code, 200, r3.content)

        # 4) Trainer assigns a routine to client -> escrow settles to trainer
        # Create a routine
        routine_api = trainer_api
        create_resp = routine_api.post(reverse("routine:routine-list"), {"name": "RL Plan", "description": "", "days": 3}, format="json")
        self.assertIn(create_resp.status_code, (200, 201, 202), create_resp.content)
        routine_id = create_resp.json().get("id")
        assign_resp = routine_api.post(reverse("routine:routine-assign-to-client", kwargs={"pk": routine_id}), {"client_id": self.client_user.id}, format="json")
        self.assertEqual(assign_resp.status_code, 200, assign_resp.content)




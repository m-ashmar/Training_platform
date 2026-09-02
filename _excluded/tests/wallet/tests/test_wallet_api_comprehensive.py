import time
from django.test import TestCase, override_settings
from django.urls import reverse
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from wallet.models import Wallet, AgentProfile, AgentAPIKey, Transaction
from wallet.security import compute_hmac_signature
from wallet.utils import get_escrow_wallet
from django.core.cache import cache


def create_user(email, username, user_type, password="pass12345"):
    User = get_user_model()
    return User.objects.create_user(
        email=email,
        username=username,
        password=password,
        user_type=user_type,
        phone_number="0000000000",
    )


def auth_client(user):
    api = APIClient()
    r = api.post(reverse("users:token_obtain_pair"), {"email": user.email, "password": "pass12345"}, format="json")
    assert r.status_code == 200, r.content
    token = r.json()["access"]
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return api


@override_settings(WALLET_DEV_MODE=False)
class WalletAPIComprehensiveTest(TestCase):
    def setUp(self):
        cache.clear()
        self.client_user = create_user("client3@example.com", "client3", "client")
        self.trainer_user = create_user("trainer3@example.com", "trainer3", "trainer")
        self.trainer_user.trainer_hourly_rate = 50
        self.trainer_user.save()
        self.agent_user = create_user("agent3@example.com", "agent3", "agent")
        self.admin_user = create_user("admin3@example.com", "admin3", "admin")
        self.admin_user.is_superuser = True
        self.admin_user.is_staff = True
        self.admin_user.save()

        # Setup agent
        ap = AgentProfile.objects.get(user=self.agent_user)
        ap.daily_limit = 500
        ap.monthly_limit = 5000
        ap.ip_allowlist = ["127.0.0.1"]
        ap.save()
        self.api_key = AgentAPIKey.objects.create(agent=ap, key_id="comp-key", hashed_key="comp-secret")

        # Ensure wallets
        Wallet.objects.get_or_create(owner=self.client_user, defaults={"owner_type": "client"})
        Wallet.objects.get_or_create(owner=self.trainer_user, defaults={"owner_type": "trainer"})

    def _agent_topup(self, amount, client_identifier=None, idem="idem-top", ip="127.0.0.1", secret=None):
        if client_identifier is None:
            client_identifier = self.client_user.email
        if secret is None:
            secret = "comp-secret"
        agent_api = auth_client(self.agent_user)
        ts = int(time.time())
        msg = f"{client_identifier}|{amount:.2f}|{ts}|{idem}"
        sig = compute_hmac_signature(secret, msg)
        r = agent_api.post(reverse("wallet:agent-topup"), {
            "client_identifier": client_identifier,
            "amount": f"{amount:.2f}",
            "idempotency_key": idem,
            "timestamp": ts,
            "signature": sig,
        }, format="json", REMOTE_ADDR=ip, HTTP_X_AGENT_AUTH=f"AgentAuth key_id={self.api_key.key_id},signature={sig},timestamp={ts}")
        return r

    def _client_request_trainer(self):
        api = auth_client(self.client_user)
        return api.post(reverse("users:client_request_trainer"), {"trainer_id": self.trainer_user.id}, format="json")

    def _trainer_pending_and_approve(self):
        trainer_api = auth_client(self.trainer_user)
        pend = trainer_api.get(reverse("users:trainer_pending_requests"))
        self.assertEqual(pend.status_code, 200, pend.content)
        req_id = pend.json()["pending_requests"][0]["request_id"]
        resp = trainer_api.post(reverse("users:trainer_respond_to_request"), {"request_id": req_id, "action": "approve"}, format="json")
        return resp

    def _trainer_create_and_assign_routine(self):
        trainer_api = auth_client(self.trainer_user)
        create = trainer_api.post(reverse("routine:routine-list"), {"name": "Comp Plan", "description": "", "days": 3}, format="json")
        self.assertIn(create.status_code, (200, 201, 202), create.content)
        routine_id = create.json()["id"]
        assign = trainer_api.post(reverse("routine:routine-assign-to-client", kwargs={"pk": routine_id}), {"client_id": self.client_user.id}, format="json")
        return assign

    def test_happy_path_escrow_hold_then_settlement(self):
        # Top up 200
        r1 = self._agent_topup(200.0, idem="idem-h1")
        self.assertEqual(r1.status_code, 200, r1.content)
        # Request trainer - should pass
        r2 = self._client_request_trainer()
        self.assertEqual(r2.status_code, 200, r2.content)
        # Approve -> funds held in escrow
        escrow_before = get_escrow_wallet().balance
        r3 = self._trainer_pending_and_approve()
        self.assertEqual(r3.status_code, 200, r3.content)
        escrow_after = get_escrow_wallet().balance
        # Trainer charge = 50 moved to escrow
        self.assertEqual(float(escrow_after - escrow_before), 50.0)
        trainer_wallet = Wallet.objects.get(owner=self.trainer_user)
        self.assertEqual(float(trainer_wallet.balance), 0.0)
        # Assign routine -> escrow settles to trainer
        r4 = self._trainer_create_and_assign_routine()
        self.assertEqual(r4.status_code, 200, r4.content)
        trainer_wallet.refresh_from_db()
        self.assertEqual(float(trainer_wallet.balance), 50.0)

    def test_client_request_blocked_if_insufficient_balance(self):
        # Top up less than charge
        r1 = self._agent_topup(10.0, idem="idem-low")
        self.assertEqual(r1.status_code, 200, r1.content)
        r2 = self._client_request_trainer()
        self.assertEqual(r2.status_code, 402, r2.content)

    def test_agent_topup_requires_valid_signature_and_ip(self):
        # Bad signature
        agent_api = auth_client(self.agent_user)
        ts = int(time.time())
        r_bad = agent_api.post(reverse("wallet:agent-topup"), {
            "client_identifier": self.client_user.email,
            "amount": "10.00",
            "idempotency_key": "idem-bad",
            "timestamp": ts,
            "signature": "bad",
        }, format="json", HTTP_X_AGENT_AUTH=f"AgentAuth key_id={self.api_key.key_id},signature=bad,timestamp={ts}")
        self.assertEqual(r_bad.status_code, 401, r_bad.content)
        # IP not allowed
        r_ip = self._agent_topup(10.0, idem="idem-ip", ip="10.0.0.1")
        self.assertEqual(r_ip.status_code, 403, r_ip.content)

    def test_agent_limits_enforced(self):
        # Daily limit 500 -> attempt 600 fails
        r = self._agent_topup(600.0, idem="idem-lim")
        self.assertEqual(r.status_code, 429, r.content)

    def test_idempotency_on_topup(self):
        r1 = self._agent_topup(20.0, idem="idem-same")
        self.assertEqual(r1.status_code, 200, r1.content)
        r2 = self._agent_topup(20.0, idem="idem-same")
        self.assertEqual(r2.status_code, 200, r2.content)
        self.assertEqual(r1.json()["reference_id"], r2.json()["reference_id"])

    def test_permissions(self):
        # Non-agent trying to topup
        api = auth_client(self.client_user)
        ts = int(time.time())
        r = api.post(reverse("wallet:agent-topup"), {
            "client_identifier": self.client_user.email,
            "amount": "10.00",
            "idempotency_key": "idem-noagent",
            "timestamp": ts,
            "signature": "bad",
        }, format="json", HTTP_X_AGENT_AUTH=f"AgentAuth key_id={self.api_key.key_id},signature=bad,timestamp={ts}")
        self.assertEqual(r.status_code, 403, r.content)
        # Non-admin reversal
        r1 = self._agent_topup(100.0, idem="idem-pr1")
        self.assertEqual(r1.status_code, 200, r1.content)
        # Make a transfer
        client_api = auth_client(self.client_user)
        r2 = client_api.post(reverse("wallet:client-transfer"), {"trainer_id": self.trainer_user.id, "amount": "5.00", "idempotency_key": "idem-tr"}, format="json")
        self.assertEqual(r2.status_code, 200, r2.content)
        ref = r2.json()["reference_id"]
        non_admin = client_api.post(reverse("wallet:admin-reversal"), {"reference_id": ref, "idempotency_key": "idem-rv1"}, format="json")
        self.assertEqual(non_admin.status_code, 403, non_admin.content)

    def test_admin_reversal(self):
        self._agent_topup(100.0, idem="idem-r1")
        client_api = auth_client(self.client_user)
        r2 = client_api.post(reverse("wallet:client-transfer"), {"trainer_id": self.trainer_user.id, "amount": "12.34", "idempotency_key": "idem-tr2"}, format="json")
        self.assertEqual(r2.status_code, 200, r2.content)
        ref = r2.json()["reference_id"]
        admin_api = auth_client(self.admin_user)
        r3 = admin_api.post(reverse("wallet:admin-reversal"), {"reference_id": ref, "idempotency_key": "idem-rv2"}, format="json")
        self.assertEqual(r3.status_code, 200, r3.content)

    def test_audit_export_and_suspicious_alerts(self):
        # Create some audit events by doing a small topup
        self._agent_topup(10.0, idem="idem-ae")
        admin_api = auth_client(self.admin_user)
        exp = admin_api.get(reverse("wallet:admin-audit-export"))
        self.assertEqual(exp.status_code, 200, exp.content)
        alerts = admin_api.get(reverse("wallet:admin-suspicious-activity"))
        self.assertEqual(alerts.status_code, 200, alerts.content)

    def test_client_cannot_request_if_already_assigned_different_trainer(self):
        # Assign client to trainer first via approval path, then another trainer should be blocked
        self._agent_topup(200.0, idem="idem-aa1")
        r2 = self._client_request_trainer()
        self.assertEqual(r2.status_code, 200)
        self._trainer_pending_and_approve()
        # Try request to another trainer
        trainer2 = create_user("trainer4@example.com", "trainer4", "trainer")
        api = auth_client(self.client_user)
        r = api.post(reverse("users:client_request_trainer"), {"trainer_id": trainer2.id}, format="json")
        self.assertEqual(r.status_code, 400, r.content)

    def test_second_trainer_cannot_approve(self):
        # First trainer approves
        self._agent_topup(200.0, idem="idem-st1")
        self._client_request_trainer()
        self._trainer_pending_and_approve()
        # Second trainer should have zero pending requests for this client
        trainer2 = create_user("trainer5@example.com", "trainer5", "trainer")
        t2_api = auth_client(trainer2)
        pend = t2_api.get(reverse("users:trainer_pending_requests"))
        self.assertEqual(pend.status_code, 200, pend.content)
        self.assertEqual(pend.json()["pending_requests_count"], 0)



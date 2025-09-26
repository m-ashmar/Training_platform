from django.db.models import Sum, Q
from django.conf import settings
from django.utils import timezone
from django.contrib.auth import get_user_model
from django.shortcuts import get_object_or_404
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from .models import Wallet, Transaction, AgentProfile, AgentAPIKey, WalletAuditLog, IdempotencyKey, move_funds_atomic
from .serializers import (
    WalletSerializer,
    TransactionSerializer,
    TopUpRequestSerializer,
    TransferRequestSerializer,
    ReversalRequestSerializer,
    AgentTopUpProxyRequestSerializer,
)
from .permissions import IsAgent, IsAdmin
from .security import (
    parse_agent_auth_header,
    verify_hmac_signature,
    is_fresh_timestamp,
    compute_hmac_signature,
)
from .throttles import ChargingRateThrottle
import uuid
import time


User = get_user_model()


def audit(event_type: str, request, payload: dict):
    WalletAuditLog.objects.create(
        event_type=event_type,
        actor=request.user if getattr(request, "user", None) and request.user.is_authenticated else None,
        request_id=getattr(request, "id", None),
        ip_address=request.META.get("REMOTE_ADDR"),
        user_agent=request.META.get("HTTP_USER_AGENT"),
        path=request.path,
        payload=payload or {},
    )


class WalletBalanceView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        wallet, _ = Wallet.objects.get_or_create(owner=request.user, defaults={"owner_type": getattr(request.user, "user_type", "client")})
        audit("wallet.balance.view", request, {"wallet_id": wallet.id})
        return Response(WalletSerializer(wallet).data)


class WalletTransactionsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        wallet, _ = Wallet.objects.get_or_create(owner=request.user, defaults={"owner_type": getattr(request.user, "user_type", "client")})
        qs = Transaction.objects.filter(Q(source_wallet=wallet) | Q(destination_wallet=wallet)).order_by("-created_at")[:200]
        audit("wallet.transactions.view", request, {"wallet_id": wallet.id, "count": qs.count()})
        return Response(TransactionSerializer(qs, many=True).data)


class AgentTopUpView(APIView):
    permission_classes = [IsAuthenticated, IsAgent]
    throttle_classes = [ChargingRateThrottle]

    def get_permissions(self):
        # In development, allow any authenticated user to hit top-up for testing
        if getattr(settings, 'WALLET_DEV_MODE', False):
            return [IsAuthenticated()]
        return super().get_permissions()

    def post(self, request):
        serializer = TopUpRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        auth_header = request.META.get("HTTP_X_AGENT_AUTH")
        parsed = parse_agent_auth_header(auth_header)
        # In dev mode, bypass strict auth freshness
        if not parsed or (not is_fresh_timestamp(parsed.timestamp) and not getattr(settings, 'WALLET_DEV_MODE', False)):
            audit("wallet.topup.auth_failed", request, {"reason": "invalid_or_stale_auth"})
            return Response({"error": "Invalid agent auth"}, status=status.HTTP_401_UNAUTHORIZED)

        try:
            api_key = AgentAPIKey.objects.select_related("agent").get(key_id=parsed.key_id, is_active=True)
        except AgentAPIKey.DoesNotExist:
            if getattr(settings, 'WALLET_DEV_MODE', False):
                agent_profile, _ = AgentProfile.objects.get_or_create(user=request.user)
                class _ApiKey:
                    agent = agent_profile
                api_key = _ApiKey()
            else:
                audit("wallet.topup.auth_failed", request, {"reason": "unknown_key"})
                return Response({"error": "Invalid credentials"}, status=status.HTTP_401_UNAUTHORIZED)

        ip = request.META.get("REMOTE_ADDR")
        if api_key.agent.ip_allowlist and ip not in api_key.agent.ip_allowlist and not getattr(settings, 'WALLET_DEV_MODE', False):
            audit("wallet.topup.denied_ip", request, {"ip": ip})
            return Response({"error": "IP not allowed"}, status=status.HTTP_403_FORBIDDEN)

        message = f"{serializer.validated_data['client_identifier']}|{serializer.validated_data['amount']}|{serializer.validated_data['timestamp']}|{serializer.validated_data['idempotency_key']}"
        if not verify_hmac_signature(api_key.hashed_key, message, parsed.signature) and not getattr(settings, 'WALLET_DEV_MODE', False):
            audit("wallet.topup.auth_failed", request, {"reason": "bad_signature"})
            return Response({"error": "Signature verification failed"}, status=status.HTTP_401_UNAUTHORIZED)

        idem_key = serializer.validated_data["idempotency_key"]
        idem, created = IdempotencyKey.objects.get_or_create(key=idem_key, defaults={"request_hash": parsed.signature, "created_by": request.user})
        if not created:
            if idem.processed and idem.response_snapshot:
                audit("wallet.topup.idempotent_hit", request, {"key": idem_key})
                return Response(idem.response_snapshot, status=status.HTTP_200_OK)
            return Response({"error": "Duplicate request"}, status=status.HTTP_409_CONFLICT)

        ident = serializer.validated_data["client_identifier"]
        client = None
        if ident.isdigit():
            client = get_object_or_404(User, id=int(ident))
        else:
            client = get_object_or_404(User, email=ident)
        if getattr(client, "user_type", None) != "client":
            return Response({"error": "Target must be a client"}, status=status.HTTP_400_BAD_REQUEST)

        client_wallet, _ = Wallet.objects.get_or_create(owner=client, defaults={"owner_type": "client"})

        agent_profile, _ = AgentProfile.objects.get_or_create(
            user=request.user,
            defaults={
                "wallet_type": "prepaid",
                "status": "active",
                "daily_limit": 0,
                "monthly_limit": 0,
            },
        )
        if agent_profile.status != "active":
            return Response({"error": "Agent not active"}, status=status.HTTP_403_FORBIDDEN)

        amount = serializer.validated_data["amount"]
        today = timezone.now().date()
        month_start = today.replace(day=1)
        day_total = Transaction.objects.filter(actor=request.user, tx_type="topup", created_at__date=today).aggregate(Sum("amount"))["amount__sum"] or 0
        month_total = Transaction.objects.filter(actor=request.user, tx_type="topup", created_at__date__gte=month_start).aggregate(Sum("amount"))["amount__sum"] or 0
        if agent_profile.daily_limit and day_total + amount > agent_profile.daily_limit:
            audit("wallet.topup.limit_block", request, {"scope": "daily", "attempt": float(amount), "used": float(day_total)})
            return Response({"error": "Daily limit exceeded"}, status=status.HTTP_429_TOO_MANY_REQUESTS)
        if agent_profile.monthly_limit and month_total + amount > agent_profile.monthly_limit:
            audit("wallet.topup.limit_block", request, {"scope": "monthly", "attempt": float(amount), "used": float(month_total)})
            return Response({"error": "Monthly limit exceeded"}, status=status.HTTP_429_TOO_MANY_REQUESTS)

        tx = move_funds_atomic(
            None,
            client_wallet,
            amount,
            actor_id=request.user.id,
            tx_type="topup",
            metadata={"agent_id": request.user.id, "api_key_id": getattr(api_key, "key_id", None)},
        )
        # refresh balance from DB to reflect updated value
        client_wallet.refresh_from_db()
        resp = {"reference_id": tx.reference_id, "balance": str(client_wallet.balance)}
        idem.processed = True
        idem.response_snapshot = resp
        idem.save(update_fields=["processed", "response_snapshot"])

        audit("wallet.topup.success", request, {"reference_id": tx.reference_id, "client_id": client.id, "amount": float(amount)})
        return Response(resp, status=status.HTTP_200_OK)


class ClientTransferToTrainerView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [ChargingRateThrottle]

    def post(self, request):
        serializer = TransferRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        idem_key = serializer.validated_data["idempotency_key"]
        idem, created = IdempotencyKey.objects.get_or_create(key=idem_key, defaults={"request_hash": idem_key, "created_by": request.user})
        if not created:
            if idem.processed and idem.response_snapshot:
                audit("wallet.transfer.idempotent_hit", request, {"key": idem_key})
                return Response(idem.response_snapshot, status=status.HTTP_200_OK)
            return Response({"error": "Duplicate request"}, status=status.HTTP_409_CONFLICT)

        if getattr(request.user, "user_type", None) != "client":
            return Response({"error": "Only clients can initiate transfers"}, status=status.HTTP_403_FORBIDDEN)

        trainer = get_object_or_404(User, id=serializer.validated_data["trainer_id"], user_type="trainer")
        client_wallet, _ = Wallet.objects.get_or_create(owner=request.user, defaults={"owner_type": "client"})
        trainer_wallet, _ = Wallet.objects.get_or_create(owner=trainer, defaults={"owner_type": "trainer"})

        amount = serializer.validated_data["amount"]
        tx = move_funds_atomic(client_wallet, trainer_wallet, amount, actor_id=request.user.id, tx_type="transfer", metadata={"trainer_id": trainer.id})
        client_wallet.refresh_from_db()
        trainer_wallet.refresh_from_db()
        resp = {"reference_id": tx.reference_id, "client_balance": str(client_wallet.balance), "trainer_balance": str(trainer_wallet.balance)}
        idem.processed = True
        idem.response_snapshot = resp
        idem.save(update_fields=["processed", "response_snapshot"])

        audit("wallet.transfer.success", request, {"reference_id": tx.reference_id, "trainer_id": trainer.id, "amount": float(amount)})
        return Response(resp, status=status.HTTP_200_OK)


class AdminReversalView(APIView):
    permission_classes = [IsAuthenticated, IsAdmin]

    def post(self, request):
        serializer = ReversalRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        idem_key = serializer.validated_data["idempotency_key"]
        idem, created = IdempotencyKey.objects.get_or_create(key=idem_key, defaults={"request_hash": idem_key, "created_by": request.user})
        if not created:
            if idem.processed and idem.response_snapshot:
                audit("wallet.reversal.idempotent_hit", request, {"key": idem_key})
                return Response(idem.response_snapshot, status=status.HTTP_200_OK)
            return Response({"error": "Duplicate request"}, status=status.HTTP_409_CONFLICT)

        original = get_object_or_404(Transaction, reference_id=serializer.validated_data["reference_id"])
        tx = move_funds_atomic(original.destination_wallet, original.source_wallet, original.amount, actor_id=request.user.id, tx_type="reversal", metadata={"original_reference": original.reference_id, "reason": serializer.validated_data.get("reason", "")})
        resp = {"reference_id": tx.reference_id}
        idem.processed = True
        idem.response_snapshot = resp
        idem.save(update_fields=["processed", "response_snapshot"])
        audit("wallet.reversal.success", request, {"reference_id": tx.reference_id, "original": original.reference_id})
        return Response(resp, status=status.HTTP_200_OK)


class AgentApiKeyCreateView(APIView):
    permission_classes = [IsAuthenticated, IsAgent]

    def get_permissions(self):
        if getattr(settings, 'WALLET_DEV_MODE', False):
            return [IsAuthenticated()]
        return super().get_permissions()

    def post(self, request):
        """
        Allows an authenticated agent to create a new API key.
        Returns key_id and secret (only shown once). Store secret server-side for HMAC verification.
        """
        agent_profile, _ = AgentProfile.objects.get_or_create(
            user=request.user,
            defaults={
                "wallet_type": "prepaid",
                "status": "active",
                "daily_limit": 0,
                "monthly_limit": 0,
            },
        )
        key_id = uuid.uuid4().hex[:16]
        secret = uuid.uuid4().hex + uuid.uuid4().hex
        # Deactivate existing active keys to ensure a single active key policy
        prev_ids = list(AgentAPIKey.objects.filter(agent=agent_profile, is_active=True).values_list("key_id", flat=True))
        if prev_ids:
            AgentAPIKey.objects.filter(agent=agent_profile, is_active=True).update(is_active=False)
            audit("wallet.agent.apikey.rotated", request, {"deactivated": prev_ids})
        # Single active key policy for ensure as well
        prev_ids = list(AgentAPIKey.objects.filter(agent=agent_profile, is_active=True).values_list("key_id", flat=True))
        if prev_ids:
            AgentAPIKey.objects.filter(agent=agent_profile, is_active=True).update(is_active=False)
            audit("wallet.agent.apikey.rotated", request, {"deactivated": prev_ids})
        AgentAPIKey.objects.create(agent=agent_profile, key_id=key_id, hashed_key=secret, is_active=True)
        audit("wallet.agent.apikey.create", request, {"key_id": key_id})
        return Response({"key_id": key_id, "secret": secret}, status=status.HTTP_201_CREATED)


class AgentApiKeyStatusView(APIView):
    """Returns whether the authenticated agent has at least one active API key.

    Does not expose secrets. Useful for onboarding flow in mobile app.
    """
    permission_classes = [IsAuthenticated, IsAgent]

    def get(self, request):
        agent_profile, _ = AgentProfile.objects.get_or_create(
            user=request.user,
            defaults={
                "wallet_type": "prepaid",
                "status": "active",
                "daily_limit": 0,
                "monthly_limit": 0,
            },
        )
        has_active = AgentAPIKey.objects.filter(agent=agent_profile, is_active=True).exists()
        return Response({"has_active": has_active})


class AgentApiKeyEnsureView(APIView):
    """Ensures the authenticated agent has an active API key.

    If none exists, creates one and returns key_id (not secret). If exists, returns the current active key_id.
    """
    permission_classes = [IsAuthenticated, IsAgent]

    def post(self, request):
        agent_profile, _ = AgentProfile.objects.get_or_create(
            user=request.user,
            defaults={
                "wallet_type": "prepaid",
                "status": "active",
                "daily_limit": 0,
                "monthly_limit": 0,
            },
        )
        api_key = AgentAPIKey.objects.filter(agent=agent_profile, is_active=True).order_by("-created_at").first()
        if api_key:
            return Response({"created": False})
        # Create without returning secret (kept server-side only)
        key_id = uuid.uuid4().hex[:16]
        secret = uuid.uuid4().hex + uuid.uuid4().hex
        AgentAPIKey.objects.create(agent=agent_profile, key_id=key_id, hashed_key=secret, is_active=True)
        audit("wallet.agent.apikey.ensure", request, {"key_id": key_id})
        return Response({"created": True}, status=status.HTTP_201_CREATED)

class AgentTopUpProxyView(APIView):
    """Server-side signing proxy for agent top-ups.

    Mobile calls this endpoint with JWT; the server signs using the stored secret and executes the top-up flow directly.
    """
    permission_classes = [IsAuthenticated, IsAgent]
    throttle_classes = [ChargingRateThrottle]

    def post(self, request):
        serializer = AgentTopUpProxyRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # Lookup or ensure agent profile
        agent_profile, _ = AgentProfile.objects.get_or_create(
            user=request.user,
            defaults={
                "wallet_type": "prepaid",
                "status": "active",
                "daily_limit": 0,
                "monthly_limit": 0,
            },
        )
        if agent_profile.status != "active":
            return Response({"error": "Agent not active"}, status=status.HTTP_403_FORBIDDEN)

        # Use latest active API key for this agent
        api_key = AgentAPIKey.objects.filter(agent=agent_profile, is_active=True).order_by("-created_at").first()
        if not api_key:
            return Response({"error": "No active API key"}, status=status.HTTP_400_BAD_REQUEST)

        client_identifier = serializer.validated_data["client_identifier"]
        amount = serializer.validated_data["amount"]
        idempotency_key = serializer.validated_data["idempotency_key"]
        ts = int(time.time())

        # Build message and compute signature server-side
        message = f"{client_identifier}|{amount}|{ts}|{idempotency_key}"
        signature = compute_hmac_signature(api_key.hashed_key, message)

        # Now execute the same logic as AgentTopUpView, but with server-built header/fields
        # Check idempotency first
        idem, created = IdempotencyKey.objects.get_or_create(
            key=idempotency_key,
            defaults={"request_hash": signature, "created_by": request.user},
        )
        if not created:
            if idem.processed and idem.response_snapshot:
                audit("wallet.topup.idempotent_hit", request, {"key": idempotency_key})
                return Response(idem.response_snapshot, status=status.HTTP_200_OK)
            return Response({"error": "Duplicate request"}, status=status.HTTP_409_CONFLICT)

        # Resolve client by id or email
        if str(client_identifier).isdigit():
            client = get_object_or_404(User, id=int(client_identifier))
        else:
            client = get_object_or_404(User, email=client_identifier)
        if getattr(client, "user_type", None) != "client":
            return Response({"error": "Target must be a client"}, status=status.HTTP_400_BAD_REQUEST)

        client_wallet, _ = Wallet.objects.get_or_create(owner=client, defaults={"owner_type": "client"})

        # Limits
        today = timezone.now().date()
        month_start = today.replace(day=1)
        day_total = Transaction.objects.filter(actor=request.user, tx_type="topup", created_at__date=today).aggregate(Sum("amount"))["amount__sum"] or 0
        month_total = Transaction.objects.filter(actor=request.user, tx_type="topup", created_at__date__gte=month_start).aggregate(Sum("amount"))["amount__sum"] or 0
        if agent_profile.daily_limit and day_total + amount > agent_profile.daily_limit:
            audit("wallet.topup.limit_block", request, {"scope": "daily", "attempt": float(amount), "used": float(day_total)})
            return Response({"error": "Daily limit exceeded"}, status=status.HTTP_429_TOO_MANY_REQUESTS)
        if agent_profile.monthly_limit and month_total + amount > agent_profile.monthly_limit:
            audit("wallet.topup.limit_block", request, {"scope": "monthly", "attempt": float(amount), "used": float(month_total)})
            return Response({"error": "Monthly limit exceeded"}, status=status.HTTP_429_TOO_MANY_REQUESTS)

        # Move funds
        tx = move_funds_atomic(None, client_wallet, amount, actor_id=request.user.id, tx_type="topup", metadata={"agent_id": request.user.id})
        client_wallet.refresh_from_db()
        resp = {"reference_id": tx.reference_id, "balance": str(client_wallet.balance)}

        idem.processed = True
        idem.response_snapshot = resp
        idem.save(update_fields=["processed", "response_snapshot"])

        # Audit success and return
        audit("wallet.topup.success", request, {"reference_id": tx.reference_id, "client_id": client.id, "amount": float(amount)})
        return Response(resp, status=status.HTTP_200_OK)

class AdminAuditExportView(APIView):
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request):
        start = request.query_params.get("start")
        end = request.query_params.get("end")
        agent_id = request.query_params.get("agent_id")
        user_id = request.query_params.get("user_id")
        qs = WalletAuditLog.objects.all()
        if start:
            qs = qs.filter(created_at__gte=start)
        if end:
            qs = qs.filter(created_at__lte=end)
        if agent_id:
            qs = qs.filter(actor_id=agent_id)
        if user_id:
            qs = qs.filter(actor_id=user_id)
        data = list(qs.values("event_type", "actor_id", "ip_address", "request_id", "path", "payload", "created_at")[:10000])
        return Response({"count": len(data), "results": data})


class AdminSuspiciousActivityView(APIView):
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request):
        window_days = int(request.query_params.get("days", 1))
        since = timezone.now() - timezone.timedelta(days=window_days)
        alerts = []
        # Heuristics: multiple failed auths, rapid topups, large amounts
        failed_auths = WalletAuditLog.objects.filter(event_type__icontains="auth_failed", created_at__gte=since).values("actor_id").annotate(cnt=Sum(1))
        for row in failed_auths:
            if row["cnt"] and row["cnt"] >= 5:
                alerts.append({"type": "failed_auth_spike", "actor_id": row["actor_id"], "count": row["cnt"]})
        large_topups = Transaction.objects.filter(tx_type="topup", created_at__gte=since, amount__gte=1000).values("actor_id").annotate(total=Sum("amount"))
        for row in large_topups:
            alerts.append({"type": "large_topups", "actor_id": row["actor_id"], "total": float(row["total"] or 0)})
        rapid_topups = Transaction.objects.filter(tx_type="topup", created_at__gte=since).values("actor_id").annotate(cnt=Sum(1))
        for row in rapid_topups:
            if row["cnt"] and row["cnt"] >= 20:
                alerts.append({"type": "rapid_topups", "actor_id": row["actor_id"], "count": row["cnt"]})
        return Response({"alerts": alerts})


class AdminDevCreateAgentView(APIView):
    permission_classes = [IsAuthenticated, IsAdmin]

    def post(self, request):
        # Only allow in development mode
        if not getattr(settings, 'WALLET_DEV_MODE', False):
            return Response({"error": "Not allowed"}, status=status.HTTP_403_FORBIDDEN)
        email = request.data.get("email")
        username = request.data.get("username") or (email.split('@')[0] if email else None)
        password = request.data.get("password") or "testpass123"
        if not email:
            return Response({"error": "email is required"}, status=status.HTTP_400_BAD_REQUEST)
        from django.contrib.auth import get_user_model
        from django.contrib.auth.hashers import make_password
        User = get_user_model()
        user, created = User.objects.get_or_create(email=email, defaults={
            "username": username or email,
            "user_type": "agent",
            "phone_number": "0000000000",
            "password": make_password(password),
        })
        if not created:
            user.user_type = "agent"
            user.password = make_password(password)
            user.save()
        audit("wallet.admin.dev.create_agent", request, {"user_id": user.id})
        return Response({"id": user.id, "email": user.email, "username": user.username}, status=status.HTTP_201_CREATED)



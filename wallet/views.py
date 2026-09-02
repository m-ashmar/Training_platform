from django.db.models import Sum, Q
from django.conf import settings
from django.utils import timezone
from django.contrib.auth import get_user_model
from django.shortcuts import get_object_or_404
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status, generics
from django.utils.translation import gettext as _
from .models import Wallet, Transaction, AgentProfile, AgentAPIKey, WalletAuditLog, IdempotencyKey, move_funds_atomic, ensure_agent_profile
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
    decrypt_secret,
    encrypt_secret,
)
from .throttles import ChargingRateThrottle
import uuid


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


def _topup_limit_error(agent_profile, actor, amount, request):
    """
    Enforce agent daily/monthly top-up caps FAIL-CLOSED. A limit of 0 means
    "no top-ups permitted", not unlimited. Returns a Response on breach, else None.
    """
    today = timezone.localdate()
    month_start = today.replace(day=1)
    day_total = Transaction.objects.filter(actor=actor, tx_type="topup", created_at__date=today).aggregate(Sum("amount"))["amount__sum"] or 0
    month_total = Transaction.objects.filter(actor=actor, tx_type="topup", created_at__date__gte=month_start).aggregate(Sum("amount"))["amount__sum"] or 0
    if day_total + amount > agent_profile.daily_limit:
        audit("wallet.topup.limit_block", request, {"scope": "daily", "attempt": float(amount), "used": float(day_total)})
        return Response({"error": _("Daily limit exceeded")}, status=status.HTTP_429_TOO_MANY_REQUESTS)
    if month_total + amount > agent_profile.monthly_limit:
        audit("wallet.topup.limit_block", request, {"scope": "monthly", "attempt": float(amount), "used": float(month_total)})
        return Response({"error": _("Monthly limit exceeded")}, status=status.HTTP_429_TOO_MANY_REQUESTS)
    return None


class WalletBalanceView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        wallet, _created = Wallet.objects.get_or_create(owner=request.user, defaults={"owner_type": getattr(request.user, "user_type", "client")})
        audit("wallet.balance.view", request, {"wallet_id": wallet.id})
        return Response(WalletSerializer(wallet).data)


class WalletTransactionsView(generics.ListAPIView):
    """Transaction history for the caller's own wallet.

    Was an APIView returning a bare JSON array hard-sliced at [:200]: the only list
    endpoint in the API without the {count, next, previous, results} envelope, and a
    user with more than 200 transactions could never reach the older ones — there was
    no next link and nothing said the list had been truncated. It is a financial
    ledger; silently hiding rows is not acceptable.
    """
    permission_classes = [IsAuthenticated]
    serializer_class = TransactionSerializer

    def get_queryset(self):
        wallet, _created = Wallet.objects.get_or_create(
            owner=self.request.user,
            defaults={"owner_type": getattr(self.request.user, "user_type", "client")},
        )
        self._wallet = wallet
        return (
            Transaction.objects
            .filter(Q(source_wallet=wallet) | Q(destination_wallet=wallet))
            .select_related("source_wallet", "destination_wallet")
            .order_by("-created_at", "-id")  # total order: -created_at alone repeats rows across pages
        )

    def list(self, request, *args, **kwargs):
        response = super().list(request, *args, **kwargs)
        # Audit the access, not the row count: counting used to run a second COUNT
        # query over the whole ledger on every view.
        audit("wallet.transactions.view", request, {"wallet_id": self._wallet.id})
        return response


class AgentTopUpView(APIView):
    permission_classes = [IsAuthenticated, IsAgent]
    throttle_classes = [ChargingRateThrottle]



    def post(self, request):
        serializer = TopUpRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        auth_header = request.META.get("HTTP_X_AGENT_AUTH")
        parsed = parse_agent_auth_header(auth_header)
        if not parsed or not is_fresh_timestamp(parsed.timestamp):
            audit("wallet.topup.auth_failed", request, {"reason": "invalid_or_stale_auth"})
            return Response({"error": _("Invalid agent auth")}, status=status.HTTP_401_UNAUTHORIZED)

        try:
            api_key = AgentAPIKey.objects.select_related("agent").get(key_id=parsed.key_id, is_active=True)
        except AgentAPIKey.DoesNotExist:
            audit("wallet.topup.auth_failed", request, {"reason": "unknown_key"})
            return Response({"error": _("Invalid credentials")}, status=status.HTTP_401_UNAUTHORIZED)

        ip = request.META.get("REMOTE_ADDR")
        if api_key.agent.ip_allowlist and ip not in api_key.agent.ip_allowlist:
            audit("wallet.topup.denied_ip", request, {"ip": ip})
            return Response({"error": _("IP not allowed")}, status=status.HTTP_403_FORBIDDEN)

        # Verify HMAC against the RAW secret (recovered from encrypted storage) —
        # never against the stored digest, which would itself be a bearer signer.
        raw_secret = decrypt_secret(api_key.secret_ciphertext) if api_key.secret_ciphertext else None
        message = f"{serializer.validated_data['client_identifier']}|{serializer.validated_data['amount']}|{serializer.validated_data['timestamp']}|{serializer.validated_data['idempotency_key']}"
        if not raw_secret or not verify_hmac_signature(raw_secret, message, parsed.signature):
            audit("wallet.topup.auth_failed", request, {"reason": "bad_signature"})
            return Response({"error": _("Signature verification failed")}, status=status.HTTP_401_UNAUTHORIZED)

        # --- Pre-side-effect validation (runs BEFORE reserving the idempotency
        #     key, so a validation failure never permanently bricks retries) ---
        ident = serializer.validated_data["client_identifier"]
        if ident.isdigit():
            client = get_object_or_404(User, id=int(ident))
        else:
            client = get_object_or_404(User, email=ident)
        if getattr(client, "user_type", None) != "client":
            return Response({"error": _("Target must be a client")}, status=status.HTTP_400_BAD_REQUEST)

        agent_profile = ensure_agent_profile(request.user)
        if agent_profile.status != "active":
            return Response({"error": _("Agent not active")}, status=status.HTTP_403_FORBIDDEN)

        amount = serializer.validated_data["amount"]
        limit_error = _topup_limit_error(agent_profile, request.user, amount, request)
        if limit_error:
            return limit_error

        # --- Reserve idempotency key only after validation passes ---
        idem_key = serializer.validated_data["idempotency_key"]
        idem, created = IdempotencyKey.objects.get_or_create(key=idem_key, defaults={"request_hash": parsed.signature, "created_by": request.user})
        if not created:
            if idem.processed and idem.response_snapshot:
                audit("wallet.topup.idempotent_hit", request, {"key": idem_key})
                return Response(idem.response_snapshot, status=status.HTTP_200_OK)
            return Response({"error": _("Duplicate request")}, status=status.HTTP_409_CONFLICT)

        client_wallet, _created = Wallet.objects.get_or_create(owner=client, defaults={"owner_type": "client"})
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

        # --- Pre-side-effect validation before reserving the idempotency key ---
        if getattr(request.user, "user_type", None) != "client":
            return Response({"error": _("Only clients can initiate transfers")}, status=status.HTTP_403_FORBIDDEN)

        trainer = get_object_or_404(User, id=serializer.validated_data["trainer_id"], user_type="trainer")

        idem_key = serializer.validated_data["idempotency_key"]
        idem, created = IdempotencyKey.objects.get_or_create(key=idem_key, defaults={"request_hash": idem_key, "created_by": request.user})
        if not created:
            if idem.processed and idem.response_snapshot:
                audit("wallet.transfer.idempotent_hit", request, {"key": idem_key})
                return Response(idem.response_snapshot, status=status.HTTP_200_OK)
            return Response({"error": _("Duplicate request")}, status=status.HTTP_409_CONFLICT)

        client_wallet, _created = Wallet.objects.get_or_create(owner=request.user, defaults={"owner_type": "client"})
        trainer_wallet, _created = Wallet.objects.get_or_create(owner=trainer, defaults={"owner_type": "trainer"})

        amount = serializer.validated_data["amount"]
        try:
            tx = move_funds_atomic(client_wallet, trainer_wallet, amount, actor_id=request.user.id, tx_type="transfer", metadata={"trainer_id": trainer.id})
        except ValueError as e:
            # No funds moved (atomic rollback) — release the key so the caller can retry.
            idem.delete()
            return Response({"error": _("Request could not be completed.")}, status=status.HTTP_400_BAD_REQUEST)
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

        # --- Pre-side-effect validation before reserving the idempotency key ---
        original = get_object_or_404(Transaction, reference_id=serializer.validated_data["reference_id"])

        # Guard: a transaction may be reversed at most once, regardless of idempotency key.
        if Transaction.objects.filter(tx_type="reversal", metadata__original_reference=original.reference_id).exists():
            return Response({"error": _("Transaction has already been reversed")}, status=status.HTTP_409_CONFLICT)

        idem_key = serializer.validated_data["idempotency_key"]
        idem, created = IdempotencyKey.objects.get_or_create(key=idem_key, defaults={"request_hash": idem_key, "created_by": request.user})
        if not created:
            if idem.processed and idem.response_snapshot:
                audit("wallet.reversal.idempotent_hit", request, {"key": idem_key})
                return Response(idem.response_snapshot, status=status.HTTP_200_OK)
            return Response({"error": _("Duplicate request")}, status=status.HTTP_409_CONFLICT)

        try:
            tx = move_funds_atomic(original.destination_wallet, original.source_wallet, original.amount, actor_id=request.user.id, tx_type="reversal", metadata={"original_reference": original.reference_id, "reason": serializer.validated_data.get("reason", "")})
        except ValueError as e:
            idem.delete()
            return Response({"error": _("Request could not be completed.")}, status=status.HTTP_400_BAD_REQUEST)
        resp = {"reference_id": tx.reference_id}
        idem.processed = True
        idem.response_snapshot = resp
        idem.save(update_fields=["processed", "response_snapshot"])
        audit("wallet.reversal.success", request, {"reference_id": tx.reference_id, "original": original.reference_id})
        return Response(resp, status=status.HTTP_200_OK)


class AgentApiKeyCreateView(APIView):
    permission_classes = [IsAuthenticated, IsAgent]

    def post(self, request):
        """
        Allows an authenticated agent to create a new API key.
        Returns key_id and secret (shown ONCE — never stored in plaintext).
        The SHA-256 hash of the secret is stored in DB for HMAC verification.
        """
        import hashlib
        agent_profile = ensure_agent_profile(request.user)
        key_id = uuid.uuid4().hex[:16]
        # Generate high-entropy secret (64 hex chars = 256 bits)
        secret = uuid.uuid4().hex + uuid.uuid4().hex
        # Non-signing lookup/compat digest; the raw secret is stored encrypted (Fernet).
        hashed_key = hashlib.sha256(secret.encode("utf-8")).hexdigest()

        # Deactivate any existing active keys (single active key policy)
        prev_ids = list(AgentAPIKey.objects.filter(agent=agent_profile, is_active=True).values_list("key_id", flat=True))
        if prev_ids:
            AgentAPIKey.objects.filter(agent=agent_profile, is_active=True).update(is_active=False)
            audit("wallet.agent.apikey.rotated", request, {"deactivated": prev_ids})

        AgentAPIKey.objects.create(
            agent=agent_profile, key_id=key_id, hashed_key=hashed_key,
            secret_ciphertext=encrypt_secret(secret), is_active=True,
        )
        audit("wallet.agent.apikey.create", request, {"key_id": key_id})
        # Return secret ONCE — client must store it securely
        return Response({"key_id": key_id, "secret": secret}, status=status.HTTP_201_CREATED)


class AgentApiKeyStatusView(APIView):
    """Returns whether the authenticated agent has at least one active API key.

    Does not expose secrets. Useful for onboarding flow in mobile app.
    """
    permission_classes = [IsAuthenticated, IsAgent]

    def get(self, request):
        agent_profile = ensure_agent_profile(request.user)
        has_active = AgentAPIKey.objects.filter(agent=agent_profile, is_active=True).exists()
        return Response({"has_active": has_active})


class AgentApiKeyEnsureView(APIView):
    """Ensures the authenticated agent has an active API key.

    If none exists, creates one and returns key_id (not secret). If exists, returns the current active key_id.
    """
    permission_classes = [IsAuthenticated, IsAgent]

    def post(self, request):
        import hashlib
        agent_profile = ensure_agent_profile(request.user)
        api_key = AgentAPIKey.objects.filter(agent=agent_profile, is_active=True).order_by("-created_at").first()
        if api_key:
            return Response({"created": False})
        # Create without returning secret (kept server-side only, encrypted at rest)
        key_id = uuid.uuid4().hex[:16]
        secret = uuid.uuid4().hex + uuid.uuid4().hex
        hashed_key = hashlib.sha256(secret.encode("utf-8")).hexdigest()
        AgentAPIKey.objects.create(
            agent=agent_profile, key_id=key_id, hashed_key=hashed_key,
            secret_ciphertext=encrypt_secret(secret), is_active=True,
        )
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

        # JWT + IsAgent is the SOLE authentication control for the mobile agent
        # flow, by design: the signing secret stays server-side and is never placed
        # on the device. No client-supplied HMAC is involved here (previously this
        # endpoint self-signed a request and never verified it — that dead code is
        # removed). Security rests on JWT auth, agent status, the fail-closed
        # top-up caps, and idempotency.
        agent_profile = ensure_agent_profile(request.user)
        if agent_profile.status != "active":
            return Response({"error": _("Agent not active")}, status=status.HTTP_403_FORBIDDEN)

        client_identifier = serializer.validated_data["client_identifier"]
        amount = serializer.validated_data["amount"]
        idempotency_key = serializer.validated_data["idempotency_key"]

        # --- Pre-side-effect validation before reserving the idempotency key ---
        if str(client_identifier).isdigit():
            client = get_object_or_404(User, id=int(client_identifier))
        else:
            client = get_object_or_404(User, email=client_identifier)
        if getattr(client, "user_type", None) != "client":
            return Response({"error": _("Target must be a client")}, status=status.HTTP_400_BAD_REQUEST)

        limit_error = _topup_limit_error(agent_profile, request.user, amount, request)
        if limit_error:
            return limit_error

        # --- Reserve idempotency key only after validation passes ---
        idem, created = IdempotencyKey.objects.get_or_create(
            key=idempotency_key,
            defaults={"request_hash": idempotency_key, "created_by": request.user},
        )
        if not created:
            if idem.processed and idem.response_snapshot:
                audit("wallet.topup.idempotent_hit", request, {"key": idempotency_key})
                return Response(idem.response_snapshot, status=status.HTTP_200_OK)
            return Response({"error": _("Duplicate request")}, status=status.HTTP_409_CONFLICT)

        client_wallet, _created = Wallet.objects.get_or_create(owner=client, defaults={"owner_type": "client"})
        tx = move_funds_atomic(None, client_wallet, amount, actor_id=request.user.id, tx_type="topup", metadata={"agent_id": request.user.id})
        client_wallet.refresh_from_db()
        resp = {"reference_id": tx.reference_id, "balance": str(client_wallet.balance)}

        idem.processed = True
        idem.response_snapshot = resp
        idem.save(update_fields=["processed", "response_snapshot"])

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
        data = list(qs.values("event_type", "actor_id", "ip_address", "request_id", "path", "payload", "created_at", "prev_hash", "entry_hash")[:10000])
        resp = {"count": len(data), "results": data}
        # Optional integrity check of the full chain: ?verify=1
        if request.query_params.get("verify") in ("1", "true", "True"):
            is_valid, first_bad_id = WalletAuditLog.verify_chain()
            resp["chain_valid"] = is_valid
            resp["first_tampered_id"] = first_bad_id
        return Response(resp)


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




import time
import hmac
import hashlib
import base64
from dataclasses import dataclass
from django.conf import settings
from cryptography.fernet import Fernet, MultiFernet


def _derive_key(material: str) -> bytes:
    """Derive a urlsafe-base64 Fernet key from arbitrary secret material."""
    digest = hashlib.sha256(material.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def _fernet_keys() -> list:
    """
    Ordered Fernet keys for agent-secret encryption. The FIRST key encrypts new
    values; ALL keys can decrypt existing ones (MultiFernet), so rotating
    DJANGO_SECRET_KEY (with the old value in SECRET_KEY_FALLBACKS) does not orphan
    already-encrypted secrets.
    """
    keys = []
    dedicated = getattr(settings, "AGENT_APIKEY_ENC_KEY", "") or ""
    if dedicated:
        keys.append(dedicated.encode("utf-8") if isinstance(dedicated, str) else dedicated)
    # Derive from the current SECRET_KEY and any rotation fallbacks.
    for sk in [settings.SECRET_KEY, *(getattr(settings, "SECRET_KEY_FALLBACKS", []) or [])]:
        if sk:
            keys.append(_derive_key(sk))
    return keys


def _fernet() -> MultiFernet:
    """MultiFernet over all valid keys — never lives in the database."""
    return MultiFernet([Fernet(k) for k in _fernet_keys()])


def encrypt_secret(raw_secret: str) -> str:
    """Encrypt an agent API secret for storage. Returns a Fernet token string."""
    return _fernet().encrypt(raw_secret.encode("utf-8")).decode("utf-8")


def decrypt_secret(token: str) -> str:
    """Recover the raw agent API secret from its stored Fernet token."""
    return _fernet().decrypt(token.encode("utf-8")).decode("utf-8")


def compute_hmac_signature(secret: str, message: str) -> str:
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_hmac_signature(secret: str, message: str, signature: str) -> bool:
    expected = compute_hmac_signature(secret, message)
    return hmac.compare_digest(expected, signature)


def is_fresh_timestamp(ts: int, window_secs: int = 60) -> bool:
    now = int(time.time())
    return abs(now - int(ts)) <= window_secs


@dataclass
class ParsedAgentAuth:
    key_id: str
    signature: str
    timestamp: int


def parse_agent_auth_header(value: str) -> ParsedAgentAuth | None:
    """Parses header: AgentAuth key_id=...,signature=...,timestamp=..."""
    if not value or not value.startswith("AgentAuth "):
        return None
    try:
        parts = value.split(" ", 1)[1].split(",")
        kv = dict(p.split("=") for p in parts)
        return ParsedAgentAuth(key_id=kv["key_id"], signature=kv["signature"], timestamp=int(kv["timestamp"]))
    except Exception:
        return None



import time
import hmac
import hashlib
from dataclasses import dataclass
from django.conf import settings


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



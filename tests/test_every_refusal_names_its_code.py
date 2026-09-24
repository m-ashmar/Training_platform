"""Every refusal a view writes names its own `code` (the owner's decision 9, 2026-09-24).

The error envelope (`training_platform/middleware.py normalize_error_envelope`) fills a
generic `code` from the HTTP status when a view names none — `bad_request`,
`permission_denied`, `conflict` — and the app, which branches on `code`, could not tell
one refusal from another. The owner decided every refusal gets its own code, before the
Flutter integration went deeper (API_CONTRACT.md §6). Six builders gave 218 refusals
their codes; this scan keeps it that way: a 4xx `Response` / `JsonResponse` whose body
is a dict written in place must carry a `code` key.

A body that is not a dict literal — a variable built above, `serializer.errors` (field
errors, answered `validation_error` with `field_errors`), a helper such as
`refusal_body(exc)` — is not judged here: the helpers carry codes and their own tests
say so.
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_PARTS = {"tests", "_excluded", "mobile", "flutter", "yalla_gym", ".venv", "migrations",
              ".claude", "node_modules"}
REFUSAL = ("HTTP_4", "400", "401", "402", "403", "404", "405", "409", "410", "413", "415",
           "422", "429")


def _status(call):
    for kw in call.keywords:
        if kw.arg == "status":
            return ast.unparse(kw.value)
    if len(call.args) >= 2:
        return ast.unparse(call.args[1])
    return ""


def _refusals_without_a_code():
    found = []
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT)
        if SKIP_PARTS & set(rel.parts) or "site-packages" in str(rel):
            continue
        try:
            tree = ast.parse(path.read_text())
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and ast.unparse(node.func) in ("Response", "JsonResponse")):
                continue
            status = _status(node)
            if not any(marker in status for marker in REFUSAL):
                continue
            body = node.args[0] if node.args else None
            if not isinstance(body, ast.Dict):
                continue
            keys = {k.value for k in body.keys if isinstance(k, ast.Constant)}
            if "code" not in keys:
                found.append(f"{rel}:{node.lineno}")
    return found


def test_every_refusal_written_in_place_names_its_code():
    missing = _refusals_without_a_code()
    assert not missing, (
        "a 4xx response names no `code`, so the app gets only the status default "
        "(the owner's decision 9, 2026-09-24; API_CONTRACT §6): " + ", ".join(missing))

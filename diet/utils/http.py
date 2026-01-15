"""
HTTP helper utilities with exponential backoff retry and error typing.
"""

from __future__ import annotations

import requests
from typing import Any, Dict
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from ..exceptions import HTTPTransientError, HTTPPermanentError


def _classify_http_error(err: requests.RequestException) -> Exception:
    if isinstance(err, requests.Timeout):
        return HTTPTransientError(str(err))
    if isinstance(err, requests.HTTPError) and err.response is not None:
        status_code = err.response.status_code
        if status_code in (429, 500, 502, 503, 504):
            return HTTPTransientError(f"HTTP {status_code}: {err}")
        return HTTPPermanentError(f"HTTP {status_code}: {err}")
    return HTTPPermanentError(str(err))


@retry(
    retry=retry_if_exception_type(HTTPTransientError),
    wait=wait_exponential_jitter(initial=1, max=30),
    stop=stop_after_attempt(5),
    reraise=True,
)
def post_json_with_retry(
    url: str,
    headers: Dict[str, str],
    payload: Dict[str, Any],
    timeout: int = 300,
) -> Dict[str, Any]:
    """
    POST JSON with exponential backoff retries for transient errors.
    Returns the parsed JSON response (dict) from the server.
    Raises HTTPPermanentError for non-retryable failures.
    """
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as err:
        mapped = _classify_http_error(err)
        # Raise mapped to trigger retry or bubble up
        raise mapped



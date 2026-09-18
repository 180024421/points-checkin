from __future__ import annotations

import json
from typing import Any

from .crypto_transport import CryptoTransportError, secure_json_request
from .settings import load_settings


def get_run_jane_api_base_url() -> str:
    # Assuming the run-jane API base URL is configured in settings
    # This needs to be consistent with how base_url is obtained in server_client.py
    settings = load_settings()
    # Replace with the actual key in settings for run-jane's base URL
    base_url = settings.get("run_jane_base_url") or "http://localhost:8687" # Default or placeholder
    if not base_url:
        raise CryptoTransportError("Run-Jane API base URL is not configured in settings.")
    return base_url

def get_license_status(device_fingerprint: str) -> dict[str, Any]:
    """
    Calls run-jane's POST /gateway/license/status API to get license status.
    Returns the decrypted payload containing tokenQuota and tokenUsed.
    """
    base_url = get_run_jane_api_base_url()
    url = f"{base_url}/api/gateway/license/status"
    scope = "gateway.license.status" # Crypto scope from run-jane's GatewayLicenseController

    payload = {"deviceFingerprint": device_fingerprint}

    response = secure_json_request(url, payload, scope)
    # The response should be the decrypted payload directly
    return response

def report_license_usage(device_fingerprint: str, tokens: int, request_id: str | None = None) -> dict[str, Any]:
    """
    Calls run-jane's POST /gateway/license/usage API to report token usage.
    """
    base_url = get_run_jane_api_base_url()
    url = f"{base_url}/api/gateway/license/usage"
    scope = "gateway.license.usage" # Crypto scope from run-jane's GatewayLicenseController

    payload = {
        "deviceFingerprint": device_fingerprint,
        "tokens": tokens,
        "estimated": False, # Assuming actual usage, not estimated
    }
    if request_id:
        payload["requestId"] = request_id

    response = secure_json_request(url, payload, scope)
    return response

from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

def lookup_card_code_with_usage(card_code: str) -> dict[str, Any]:
    """
    Calls run-jane's GET /api/cardSale/open/card-code/lookup-with-usage API
    to get card code details including usageLimit.
    """
    base_url = get_run_jane_api_base_url()
    settings = load_settings()
    api_token = settings.get("run_jane_api_token")

    if not api_token:
        raise ValueError("Run-Jane API token is not configured in settings.")

    url = f"{base_url}/api/cardSale/open/card-code/lookup-with-usage?cardCode={card_code}"
    
    headers = {
        "X-Card-Api-Token": api_token,
        "Accept": "application/json",
        "User-Agent": "PointsCheckinTool/1.0",
    }

    req = Request(url, headers=headers, method="GET")

    try:
        with urlopen(req, timeout=10) as resp:
            raw_response = resp.read()
            response_data = json.loads(raw_response.decode("utf-8"))
            return response_data
    except HTTPError as e:
        error_message = e.read().decode("utf-8")
        try:
            error_json = json.loads(error_message)
            raise CryptoTransportError(f"Run-Jane API error: {error_json.get('message', 'Unknown error')}") from e
        except json.JSONDecodeError:
            raise CryptoTransportError(f"Run-Jane API HTTP error: {e.code} - {error_message}") from e
    except URLError as e:
        raise CryptoTransportError(f"Run-Jane API network error: {e.reason}") from e
    except json.JSONDecodeError as e:
        raise CryptoTransportError(f"Run-Jane API response JSON decode error: {e}") from e
    except Exception as e:
        raise CryptoTransportError(f"Run-Jane API unexpected error: {e}") from e


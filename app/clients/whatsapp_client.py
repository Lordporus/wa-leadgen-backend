from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import requests

from app.core import config

logger = logging.getLogger(__name__)

_GRAPH_HOST = "graph.facebook.com"
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*|\d+)\s*\}\}")
_MEDIA_PARAMETER_TYPES = {"image", "video", "document"}
_SUPPORTED_PARAMETER_TYPES = {
    "text",
    "image",
    "video",
    "document",
    "payload",
}


class MetaVerificationError(RuntimeError):
    """Meta evidence could not be verified safely."""


class MetaPermissionError(MetaVerificationError):
    """The tenant token cannot perform the requested Meta operation."""


class MetaTransportError(MetaVerificationError):
    """Meta could not be reached within the configured operating bounds."""


@dataclass(frozen=True)
class WhatsAppTenantCredentials:
    client_id: int
    access_token: str
    waba_id: str
    phone_number_id: str
    graph_api_version: str
    request_timeout_seconds: float


@dataclass(frozen=True)
class MetaTemplateVerification:
    template_id: str
    name: str
    language: str
    status: str
    category: str
    component_signature: list[dict[str, Any]]
    variable_count: int
    waba_id: str
    phone_number_id: str


class WhatsAppClient:
    def __init__(self) -> None:
        # This process-level limiter remains a conservative provider guardrail.
        # Tenant policy limits are enforced transactionally in whatsapp_policy.
        self.daily_cap = 50
        self.sends_today = 0
        self.current_date = datetime.now(timezone.utc).date()

    def _check_rate_limit(self) -> bool:
        """Apply the existing conservative process-level provider guardrail."""
        today = datetime.now(timezone.utc).date()
        if today > self.current_date:
            self.current_date = today
            self.sends_today = 0

        if self.sends_today >= self.daily_cap:
            logger.warning(
                "WhatsApp process-level daily send cap reached (%s).",
                self.daily_cap,
            )
            return False

        if config.WHATSAPP_SIMULATE_HUMAN_DELAY:
            delay = random.uniform(3.0, 10.0)
            logger.info("Applying WhatsApp send delay of %.2fs.", delay)
            time.sleep(delay)

        self.sends_today += 1
        return True

    def send_message(
        self,
        to_phone: str,
        text: str,
        *,
        credentials: WhatsAppTenantCredentials,
    ) -> str | None:
        if not self._check_rate_limit():
            return None
        payload: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to_phone,
            "type": "text",
            "text": {"preview_url": False, "body": text},
        }
        response = self._request(
            "post",
            self._graph_url(credentials, f"{credentials.phone_number_id}/messages"),
            credentials=credentials,
            json=payload,
        )
        data = response.json()
        return data.get("messages", [{}])[0].get("id")

    def send_template(
        self,
        to_phone: str,
        template_name: str,
        language_code: str = "en",
        *,
        components: list[dict[str, Any]] | None = None,
        credentials: WhatsAppTenantCredentials,
    ) -> str | None:
        if not self._check_rate_limit():
            return None
        template: dict[str, Any] = {
            "name": template_name,
            "language": {"code": language_code},
        }
        if components:
            template["components"] = components
        payload: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to_phone,
            "type": "template",
            "template": template,
        }
        response = self._request(
            "post",
            self._graph_url(credentials, f"{credentials.phone_number_id}/messages"),
            credentials=credentials,
            json=payload,
        )
        data = response.json()
        return data.get("messages", [{}])[0].get("id")

    def submit_template(self, *_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError(
            "Production template submission is disabled; Phase 7 only verifies "
            "templates already approved by Meta."
        )

    def get_template(
        self,
        name: str,
        *,
        credentials: WhatsAppTenantCredentials,
    ) -> dict[str, Any] | None:
        rows = self._get_all_pages(
            self._graph_url(credentials, f"{credentials.waba_id}/message_templates"),
            credentials=credentials,
            params={
                "name": name,
                "fields": (
                    "id,name,status,category,language,parameter_format,components"
                ),
                "limit": "100",
            },
        )
        matches = [row for row in rows if row.get("name") == name]
        return matches[0] if len(matches) == 1 else None

    def verify_template(
        self,
        *,
        tenant_phone_number_id: str,
        name: str,
        language: str,
        credentials: WhatsAppTenantCredentials,
    ) -> MetaTemplateVerification:
        """Verify phone ownership and template evidence for one tenant identity."""
        if credentials.phone_number_id != tenant_phone_number_id:
            raise MetaVerificationError(
                "Tenant phone does not match the bound Meta sending identity"
            )

        phone_rows = self._get_all_pages(
            self._graph_url(credentials, f"{credentials.waba_id}/phone_numbers"),
            credentials=credentials,
            params={"fields": "id", "limit": "100"},
        )
        owned_phone_ids = {
            str(item["id"]) for item in phone_rows if item.get("id") is not None
        }
        if tenant_phone_number_id not in owned_phone_ids:
            raise MetaVerificationError(
                "Tenant phone number is not owned by the bound WABA"
            )

        template_rows = self._get_all_pages(
            self._graph_url(credentials, f"{credentials.waba_id}/message_templates"),
            credentials=credentials,
            params={
                "name": name,
                "fields": (
                    "id,name,status,category,language,parameter_format,components"
                ),
                "limit": "100",
            },
        )
        matches = [
            item
            for item in template_rows
            if item.get("name") == name and item.get("language") == language
        ]
        if len(matches) != 1:
            raise MetaVerificationError(
                "Meta template name and language did not match uniquely"
            )
        item = matches[0]
        signature = component_signature_from_meta(item)
        return MetaTemplateVerification(
            template_id=str(item.get("id") or ""),
            name=str(item.get("name") or ""),
            language=str(item.get("language") or ""),
            status=str(item.get("status") or "").lower(),
            category=str(item.get("category") or "").lower(),
            component_signature=signature,
            variable_count=component_parameter_count(signature),
            waba_id=credentials.waba_id,
            phone_number_id=tenant_phone_number_id,
        )

    def _get_all_pages(
        self,
        url: str,
        *,
        credentials: WhatsAppTenantCredentials,
        params: dict[str, str],
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        next_url: str | None = url
        next_params: dict[str, str] | None = params
        pages = 0
        while next_url:
            pages += 1
            if pages > config.WHATSAPP_META_MAX_PAGES:
                raise MetaVerificationError("Meta pagination exceeded the safe limit")
            self._validate_graph_url(next_url, credentials)
            response = self._request(
                "get",
                next_url,
                credentials=credentials,
                params=next_params,
            )
            payload = response.json()
            data = payload.get("data")
            if not isinstance(data, list):
                raise MetaVerificationError("Meta returned an invalid paginated response")
            rows.extend(item for item in data if isinstance(item, dict))
            paging = payload.get("paging")
            candidate = paging.get("next") if isinstance(paging, dict) else None
            next_url = candidate if isinstance(candidate, str) and candidate else None
            next_params = None
        return rows

    def _request(
        self,
        method: str,
        url: str,
        *,
        credentials: WhatsAppTenantCredentials,
        **kwargs: Any,
    ) -> requests.Response:
        headers = dict(kwargs.pop("headers", {}))
        headers["Authorization"] = f"Bearer {credentials.access_token}"
        if method == "post":
            headers["Content-Type"] = "application/json"
        try:
            response = requests.request(
                method,
                url,
                headers=headers,
                timeout=credentials.request_timeout_seconds,
                **kwargs,
            )
        except requests.exceptions.Timeout as exc:
            raise MetaTransportError("Meta request timed out") from exc
        except requests.exceptions.RequestException as exc:
            raise MetaTransportError("Meta request failed") from exc
        if response.status_code in {401, 403}:
            raise MetaPermissionError(
                "Meta rejected the tenant credential or required permission"
            )
        try:
            response.raise_for_status()
        except requests.exceptions.RequestException as exc:
            raise MetaTransportError(
                f"Meta request failed with HTTP {response.status_code}"
            ) from exc
        return response

    @staticmethod
    def _graph_url(credentials: WhatsAppTenantCredentials, path: str) -> str:
        return (
            f"https://{_GRAPH_HOST}/{credentials.graph_api_version}/"
            f"{path.lstrip('/')}"
        )

    @staticmethod
    def _validate_graph_url(
        url: str,
        credentials: WhatsAppTenantCredentials,
    ) -> None:
        parsed = urlparse(url)
        expected_prefix = f"/{credentials.graph_api_version}/"
        if (
            parsed.scheme != "https"
            or parsed.netloc != _GRAPH_HOST
            or not parsed.path.startswith(expected_prefix)
        ):
            raise MetaVerificationError("Meta pagination returned an unsafe URL")


def component_signature_from_meta(
    template: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return a content-minimal, component-aware template signature."""
    raw_components = template.get("components")
    if not isinstance(raw_components, list):
        raise MetaVerificationError("Meta template components are missing")
    signature: list[dict[str, Any]] = []
    for raw_component in raw_components:
        if not isinstance(raw_component, dict):
            raise MetaVerificationError("Meta template component is invalid")
        component_type = str(raw_component.get("type") or "").lower()
        if not component_type:
            raise MetaVerificationError("Meta template component type is missing")
        if component_type == "buttons":
            buttons = raw_component.get("buttons")
            if not isinstance(buttons, list):
                raise MetaVerificationError("Meta template buttons are invalid")
            for button_index, raw_button in enumerate(buttons):
                if not isinstance(raw_button, dict):
                    raise MetaVerificationError("Meta template button is invalid")
                sub_type = str(raw_button.get("type") or "").lower()
                parameters = _text_parameters(raw_button.get("url"))
                if sub_type == "quick_reply":
                    parameters = [{"key": "payload", "type": "payload"}]
                signature.append(
                    {
                        "type": "button",
                        "sub_type": sub_type,
                        "index": str(button_index),
                        "parameters": parameters,
                    }
                )
            continue

        entry: dict[str, Any] = {"type": component_type, "parameters": []}
        if component_type == "header":
            header_format = str(raw_component.get("format") or "text").lower()
            entry["format"] = header_format
            if header_format in _MEDIA_PARAMETER_TYPES:
                entry["parameters"].append(
                    {"key": "media", "type": header_format}
                )
        entry["parameters"].extend(_text_parameters(raw_component.get("text")))
        signature.append(entry)
    normalized = normalize_component_signature(signature)
    parameter_format = str(
        template.get("parameter_format") or "POSITIONAL"
    ).strip().lower()
    if parameter_format not in {"named", "positional"}:
        raise MetaVerificationError("Meta template parameter format is invalid")
    text_keys = [
        parameter["key"]
        for component in normalized
        for parameter in component["parameters"]
        if parameter["type"] == "text"
    ]
    if parameter_format == "named" and any(
        key.isdigit() for key in text_keys
    ):
        raise MetaVerificationError(
            "Meta named template contains positional placeholders"
        )
    if parameter_format == "positional" and any(
        not key.isdigit() for key in text_keys
    ):
        raise MetaVerificationError(
            "Meta positional template contains named placeholders"
        )
    return normalized


def normalize_component_signature(
    signature: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen_components: set[tuple[str, str | None]] = set()
    for raw in signature:
        if not isinstance(raw, dict):
            raise ValueError("Template component signature must contain objects")
        component_type = str(raw.get("type") or "").strip().lower()
        if component_type not in {"header", "body", "footer", "button"}:
            raise ValueError(f"Unsupported template component type: {component_type}")
        index = str(raw.get("index")) if raw.get("index") is not None else None
        component_key = (component_type, index)
        if component_key in seen_components:
            raise ValueError("Template component signature contains duplicates")
        seen_components.add(component_key)
        entry: dict[str, Any] = {"type": component_type, "parameters": []}
        if component_type == "header":
            entry["format"] = str(raw.get("format") or "text").strip().lower()
        if component_type == "button":
            if index is None:
                raise ValueError("Button components require an index")
            entry["index"] = index
            entry["sub_type"] = str(raw.get("sub_type") or "").strip().lower()
            if not entry["sub_type"]:
                raise ValueError("Button components require a sub_type")
        raw_parameters = raw.get("parameters") or []
        if not isinstance(raw_parameters, list):
            raise ValueError("Template component parameters must be a list")
        seen_parameters: set[tuple[str, str]] = set()
        for raw_parameter in raw_parameters:
            if not isinstance(raw_parameter, dict):
                raise ValueError("Template parameter signature must be an object")
            key = str(raw_parameter.get("key") or "").strip()
            parameter_type = str(raw_parameter.get("type") or "").strip().lower()
            if not key or parameter_type not in _SUPPORTED_PARAMETER_TYPES:
                raise ValueError("Template parameter key or type is invalid")
            marker = (key, parameter_type)
            if marker in seen_parameters:
                continue
            seen_parameters.add(marker)
            entry["parameters"].append({"key": key, "type": parameter_type})
        normalized.append(entry)
    return normalized


def component_parameter_count(signature: list[dict[str, Any]]) -> int:
    return sum(len(component.get("parameters") or []) for component in signature)


def build_template_send_components(
    signature: list[dict[str, Any]],
    values: list[Any] | dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Bind values to their verified Meta component locations and types."""
    normalized = normalize_component_signature(signature)
    slots = [
        (component, parameter)
        for component in normalized
        for parameter in component["parameters"]
    ]
    if not slots:
        if values not in (None, [], {}):
            raise ValueError("Template does not accept parameters")
        return []

    if isinstance(values, list):
        if len(values) != len(slots):
            raise ValueError("Template parameter count does not match verification")
        bound_values = values
    elif isinstance(values, dict):
        bound_values = []
        for component, parameter in slots:
            lookup_key = _parameter_lookup_key(component, parameter)
            if lookup_key not in values:
                raise ValueError(f"Missing template parameter: {lookup_key}")
            bound_values.append(values[lookup_key])
        if set(values) != {
            _parameter_lookup_key(component, parameter)
            for component, parameter in slots
        }:
            raise ValueError("Unknown template parameter supplied")
    else:
        raise ValueError("Template parameters are required")

    result: list[dict[str, Any]] = []
    value_index = 0
    for component in normalized:
        if not component["parameters"]:
            continue
        rendered: dict[str, Any] = {"type": component["type"], "parameters": []}
        if component["type"] == "button":
            rendered["sub_type"] = component["sub_type"]
            rendered["index"] = component["index"]
        for parameter in component["parameters"]:
            rendered["parameters"].append(
                _render_parameter(
                    parameter["type"],
                    bound_values[value_index],
                    key=parameter["key"],
                )
            )
            value_index += 1
        result.append(rendered)
    return result


def _text_parameters(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, str):
        return []
    keys: list[str] = []
    for match in _PLACEHOLDER_RE.finditer(value):
        key = match.group(1)
        if key not in keys:
            keys.append(key)
    return [{"key": key, "type": "text"} for key in keys]


def _parameter_lookup_key(
    component: dict[str, Any],
    parameter: dict[str, str],
) -> str:
    if component["type"] == "button":
        return f"button:{component['index']}:{parameter['key']}"
    return f"{component['type']}:{parameter['key']}"


def _render_parameter(
    parameter_type: str,
    value: Any,
    *,
    key: str,
) -> dict[str, Any]:
    if parameter_type in {"text", "payload"}:
        field = "text" if parameter_type == "text" else "payload"
        rendered = {"type": parameter_type, field: str(value)}
        if parameter_type == "text" and not key.isdigit():
            rendered["parameter_name"] = key
        return rendered
    if parameter_type in {"image", "video", "document"}:
        payload = value if isinstance(value, dict) else {"link": str(value)}
        return {"type": parameter_type, parameter_type: payload}
    raise ValueError(f"Unsupported template parameter type: {parameter_type}")

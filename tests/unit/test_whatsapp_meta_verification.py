from __future__ import annotations

from types import SimpleNamespace

import pytest
import requests

from app.clients.whatsapp_client import (
    MetaPermissionError,
    MetaTransportError,
    MetaVerificationError,
    WhatsAppClient,
    WhatsAppTenantCredentials,
    build_template_send_components,
    component_signature_from_meta,
)

_VERIFY_TEMPLATE = WhatsAppClient.verify_template
_SEND_TEMPLATE = WhatsAppClient.send_template


class _Response:
    def __init__(self, payload, *, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def _credentials(*, client_id=1, waba_id="waba-1", phone_id="phone-1"):
    return WhatsAppTenantCredentials(
        client_id=client_id,
        access_token=f"test-token-{client_id}",
        waba_id=waba_id,
        phone_number_id=phone_id,
        graph_api_version="v25.0",
        request_timeout_seconds=7.5,
    )


def test_meta_verification_paginates_and_binds_waba_phone_and_template(
    monkeypatch,
):
    responses = iter(
        [
            _Response(
                {
                    "data": [{"id": "phone-other"}],
                    "paging": {
                        "next": (
                            "https://graph.facebook.com/v25.0/"
                            "waba-1/phone_numbers?after=phone"
                        )
                    },
                }
            ),
            _Response({"data": [{"id": "phone-1"}]}),
            _Response(
                {
                    "data": [
                        {
                            "id": "template-other",
                            "name": "other",
                            "language": "en",
                        }
                    ],
                    "paging": {
                        "next": (
                            "https://graph.facebook.com/v25.0/"
                            "waba-1/message_templates?after=template"
                        )
                    },
                }
            ),
            _Response(
                {
                    "data": [
                        {
                            "id": "template-1",
                            "name": "follow_up",
                            "language": "en",
                            "status": "APPROVED",
                            "category": "UTILITY",
                            "parameter_format": "POSITIONAL",
                            "components": [
                                {
                                    "type": "HEADER",
                                    "format": "TEXT",
                                    "text": "Reference {{1}}",
                                },
                                {
                                    "type": "BODY",
                                    "text": "Hello {{1}}, item {{2}}",
                                },
                                {
                                    "type": "BUTTONS",
                                    "buttons": [
                                        {
                                            "type": "URL",
                                            "url": "https://example.invalid/{{1}}",
                                        },
                                        {"type": "QUICK_REPLY", "text": "Done"},
                                    ],
                                },
                            ],
                        }
                    ]
                }
            ),
        ]
    )
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append(SimpleNamespace(method=method, url=url, kwargs=kwargs))
        return next(responses)

    monkeypatch.setattr(
        "app.clients.whatsapp_client.requests.request",
        fake_request,
    )
    result = _VERIFY_TEMPLATE(
        WhatsAppClient(),
        tenant_phone_number_id="phone-1",
        name="follow_up",
        language="en",
        credentials=_credentials(),
    )

    assert result.template_id == "template-1"
    assert result.waba_id == "waba-1"
    assert result.phone_number_id == "phone-1"
    assert result.status == "approved"
    assert result.category == "utility"
    # {{1}} appears independently in header, body, and URL button.
    assert result.variable_count == 5
    assert len(calls) == 4
    assert all(call.kwargs["timeout"] == 7.5 for call in calls)
    assert all(
        call.kwargs["headers"]["Authorization"] == "Bearer test-token-1"
        for call in calls
    )
    assert all("/v25.0/waba-1/" in call.url for call in calls)


def test_meta_verification_rejects_cross_identity_before_network(monkeypatch):
    monkeypatch.setattr(
        "app.clients.whatsapp_client.requests.request",
        lambda *_args, **_kwargs: pytest.fail(
            "cross-identity must fail before Meta call"
        ),
    )
    with pytest.raises(
        MetaVerificationError,
        match="bound Meta sending identity",
    ):
        _VERIFY_TEMPLATE(
            WhatsAppClient(),
            tenant_phone_number_id="phone-2",
            name="follow_up",
            language="en",
            credentials=_credentials(),
        )


def test_meta_verification_rejects_phone_not_owned_by_bound_waba(monkeypatch):
    monkeypatch.setattr(
        "app.clients.whatsapp_client.requests.request",
        lambda *_args, **_kwargs: _Response(
            {"data": [{"id": "phone-other"}]}
        ),
    )
    with pytest.raises(MetaVerificationError, match="not owned"):
        _VERIFY_TEMPLATE(
            WhatsAppClient(),
            tenant_phone_number_id="phone-1",
            name="follow_up",
            language="en",
            credentials=_credentials(),
        )


def test_meta_permission_failure_and_timeout_fail_closed(monkeypatch):
    monkeypatch.setattr(
        "app.clients.whatsapp_client.requests.request",
        lambda *_args, **_kwargs: _Response({}, status_code=403),
    )
    with pytest.raises(MetaPermissionError):
        _VERIFY_TEMPLATE(
            WhatsAppClient(),
            tenant_phone_number_id="phone-1",
            name="follow_up",
            language="en",
            credentials=_credentials(),
        )

    def timeout(*_args, **_kwargs):
        raise requests.Timeout("offline timeout")

    monkeypatch.setattr(
        "app.clients.whatsapp_client.requests.request",
        timeout,
    )
    with pytest.raises(MetaTransportError, match="timed out"):
        _VERIFY_TEMPLATE(
            WhatsAppClient(),
            tenant_phone_number_id="phone-1",
            name="follow_up",
            language="en",
            credentials=_credentials(),
        )


def test_meta_rejects_unsafe_pagination_url(monkeypatch):
    monkeypatch.setattr(
        "app.clients.whatsapp_client.requests.request",
        lambda *_args, **_kwargs: _Response(
            {
                "data": [],
                "paging": {
                    "next": "https://attacker.invalid/v25.0/steal-token"
                },
            }
        ),
    )
    with pytest.raises(MetaVerificationError, match="unsafe URL"):
        _VERIFY_TEMPLATE(
            WhatsAppClient(),
            tenant_phone_number_id="phone-1",
            name="follow_up",
            language="en",
            credentials=_credentials(),
        )


def test_component_signature_and_send_payload_preserve_exact_locations():
    signature = component_signature_from_meta(
        {
            "parameter_format": "NAMED",
            "components": [
                {"type": "HEADER", "format": "IMAGE"},
                {
                    "type": "BODY",
                    "text": "Hello {{first_name}}: {{reference}}",
                },
                {
                    "type": "BUTTONS",
                    "buttons": [
                        {
                            "type": "URL",
                            "url": "https://example.invalid/{{reference}}",
                        },
                        {"type": "QUICK_REPLY", "text": "Confirm"},
                    ],
                },
            ],
        }
    )

    components = build_template_send_components(
        signature,
        {
            "header:media": {"link": "https://example.invalid/image.jpg"},
            "body:first_name": "Ada",
            "body:reference": "REF-1",
            "button:0:reference": "REF-1",
            "button:1:payload": "confirm-ref-1",
        },
    )

    assert components == [
        {
            "type": "header",
            "parameters": [
                {
                    "type": "image",
                    "image": {
                        "link": "https://example.invalid/image.jpg"
                    },
                }
            ],
        },
        {
            "type": "body",
            "parameters": [
                {
                    "type": "text",
                    "text": "Ada",
                    "parameter_name": "first_name",
                },
                {
                    "type": "text",
                    "text": "REF-1",
                    "parameter_name": "reference",
                },
            ],
        },
        {
            "type": "button",
            "parameters": [
                {
                    "type": "text",
                    "text": "REF-1",
                    "parameter_name": "reference",
                }
            ],
            "sub_type": "url",
            "index": "0",
        },
        {
            "type": "button",
            "parameters": [
                {"type": "payload", "payload": "confirm-ref-1"}
            ],
            "sub_type": "quick_reply",
            "index": "1",
        },
    ]


def test_template_send_uses_component_payload_and_tenant_phone(monkeypatch):
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return _Response({"messages": [{"id": "wamid.accepted"}]})

    monkeypatch.setattr(
        "app.clients.whatsapp_client.requests.request",
        fake_request,
    )
    message_id = _SEND_TEMPLATE(
        WhatsAppClient(),
        "15550000001",
        "follow_up",
        "en",
        components=[
            {
                "type": "body",
                "parameters": [{"type": "text", "text": "Ada"}],
            }
        ],
        credentials=_credentials(phone_id="tenant-phone"),
    )
    assert message_id == "wamid.accepted"
    assert calls[0][1].endswith("/tenant-phone/messages")
    assert calls[0][2]["json"]["template"]["components"][0]["type"] == "body"

import importlib

import pytest
from requests.exceptions import ConnectionError

def test_send_message_propagates_retryable_provider_failure(monkeypatch):
    # The offline suite replaces provider methods globally; reload this narrow
    # module so this test exercises the real implementation with a fake HTTP
    # transport, never the network.
    module = importlib.reload(importlib.import_module("app.clients.whatsapp_client"))
    client = module.WhatsAppClient()
    monkeypatch.setattr(client, "_check_rate_limit", lambda: True)
    monkeypatch.setattr(
        module.requests,
        "post",
        lambda *args, **kwargs: (_ for _ in ()).throw(ConnectionError("offline")),
    )

    with pytest.raises(ConnectionError, match="offline"):
        client.send_message("15550000000", "hello")

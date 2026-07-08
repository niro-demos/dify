from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from services.tools import api_tools_manage_service as service_module
from services.tools.api_tools_manage_service import ApiToolManageService


def test_remote_schema_fetch_uses_ssrf_protected_client(monkeypatch: pytest.MonkeyPatch):
    response = SimpleNamespace(status_code=200, text='{"openapi":"3.1.0","paths":{}}')
    ssrf_get = Mock(return_value=response)

    def raw_http_get(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("remote schema fetch must not use the raw httpx client")

    monkeypatch.setattr(service_module, "ssrf_proxy", SimpleNamespace(get=ssrf_get), raising=False)
    monkeypatch.setattr(service_module, "get", raw_http_get, raising=False)
    monkeypatch.setattr(ApiToolManageService, "parser_api_schema", lambda schema: None)

    result = ApiToolManageService.get_api_tool_provider_remote_schema(
        "user-1",
        "tenant-1",
        "https://example.com/openapi.json",
    )

    assert result == {"schema": response.text}
    ssrf_get.assert_called_once()
    assert ssrf_get.call_args.args == ("https://example.com/openapi.json",)
    assert ssrf_get.call_args.kwargs["timeout"] == 10

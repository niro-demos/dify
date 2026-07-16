"""Authorization regression tests for owner/admin-only tool-provider endpoints.

Shared root cause across three findings: several workspace-configuration
mutation endpoints in `controllers/console/workspace/tool_providers.py` were
missing the `is_admin_or_owner_required` gate that every sibling owner/admin-only
endpoint in this module carries, so any authenticated normal-role tenant member
could perform them:

- TC-44A7982B: `ToolOAuthCustomClient.delete` -- delete the team's configured
  custom OAuth client for a builtin tool provider.
- TC-2981828D: `ToolProviderMCPApi.post` / `.put` / `.delete` -- create, update,
  or delete workspace-wide MCP tool-server connections. `rbac_permission_required`
  alone is a documented no-op when `RBAC_ENABLED` is `False` (the self-hosted
  default), so this endpoint had no real gate at all in that mode.
- TC-B66EF0FA: `ToolBuiltinProviderAddApi.post` -- add a team-wide builtin tool
  credential, unlike its sibling `update`/`delete` endpoints on the same
  resource which already carried the gate.

These tests reimport the controller module fresh with only the
transport/plumbing decorators (`login_required`, `setup_required`,
`account_initialization_required`) stubbed to identity, while keeping
`is_admin_or_owner_required` and `rbac_permission_required` real -- so the
assertions exercise the actual authorization gate rather than a mock of it.
"""

from __future__ import annotations

import importlib
import sys
from types import ModuleType
from unittest.mock import MagicMock

import pytest
from flask import Flask
from werkzeug.exceptions import Forbidden

from models.account import Account, TenantAccountRole

MODULE_NAME = "controllers.console.workspace.tool_providers"


@pytest.fixture
def app() -> Flask:
    flask_app = Flask(__name__)
    flask_app.config["TESTING"] = True
    return flask_app


def _identity(func):
    return func


@pytest.fixture
def tool_providers_module(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Reimport the controller with plumbing decorators neutralized to identity.

    `is_admin_or_owner_required` and `rbac_permission_required` are left real.
    """
    from controllers.console import console_ns, wraps
    from libs import login

    monkeypatch.setattr(login, "login_required", _identity)
    monkeypatch.setattr(wraps, "setup_required", _identity)
    monkeypatch.setattr(wraps, "account_initialization_required", _identity)
    monkeypatch.setattr(wraps, "with_current_tenant_id", _identity)
    monkeypatch.setattr(wraps, "with_current_user", _identity)

    def _noop_route(*_args, **_kwargs):
        def _decorator(cls):
            return cls

        return _decorator

    # Reimporting re-runs the module's `@console_ns.route(...)` class
    # decorators; neutralize registration so this doesn't re-register the same
    # Flask endpoint name a second time on the shared, process-wide `console_ns`
    # (which would otherwise break app construction in unrelated tests).
    monkeypatch.setattr(console_ns, "route", _noop_route)

    sys.modules.pop(MODULE_NAME, None)
    return importlib.import_module(MODULE_NAME)


def _make_account(role: TenantAccountRole, account_id: str = "user-1") -> Account:
    account = Account(name="Test User", email=f"{account_id}@example.com")
    account.id = account_id
    account.role = role
    return account


def _prepare_context(monkeypatch: pytest.MonkeyPatch, role: TenantAccountRole) -> Account:
    from controllers.console import wraps
    from libs import login

    user = _make_account(role)
    monkeypatch.setattr(wraps, "current_account_with_tenant", lambda: (user, "tenant-123"))
    monkeypatch.setattr(wraps.dify_config, "RBAC_ENABLED", False)
    monkeypatch.setattr(login, "_get_user", lambda: user)
    return user


class TestToolOAuthCustomClientDeleteAuthorization:
    """TC-44A7982B: only an owner/admin may delete the team's custom OAuth client."""

    def test_normal_member_delete_is_rejected(
        self, app: Flask, tool_providers_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ):
        _prepare_context(monkeypatch, TenantAccountRole.NORMAL)
        service_mock = MagicMock(return_value={"result": "success"})
        monkeypatch.setattr(
            tool_providers_module.BuiltinToolManageService, "delete_custom_oauth_client_params", service_mock
        )

        with app.test_request_context("/oauth/custom-client", method="DELETE"):
            api = tool_providers_module.ToolOAuthCustomClient()
            with pytest.raises(Forbidden):
                api.delete(current_tenant_id="tenant-123", provider="code")

        service_mock.assert_not_called()

    def test_owner_delete_succeeds(
        self, app: Flask, tool_providers_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ):
        _prepare_context(monkeypatch, TenantAccountRole.OWNER)
        service_mock = MagicMock(return_value={"result": "success"})
        monkeypatch.setattr(
            tool_providers_module.BuiltinToolManageService, "delete_custom_oauth_client_params", service_mock
        )

        with app.test_request_context("/oauth/custom-client", method="DELETE"):
            api = tool_providers_module.ToolOAuthCustomClient()
            result = api.delete(current_tenant_id="tenant-123", provider="code")

        assert result["result"] == "success"
        service_mock.assert_called_once_with(tenant_id="tenant-123", provider="code")


class TestToolBuiltinProviderAddApiAuthorization:
    """TC-B66EF0FA: only an owner/admin may add a team-wide builtin tool credential."""

    def test_normal_member_add_is_rejected(
        self, app: Flask, tool_providers_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ):
        _prepare_context(monkeypatch, TenantAccountRole.NORMAL)
        service_mock = MagicMock(return_value={"result": "success"})
        monkeypatch.setattr(tool_providers_module.BuiltinToolManageService, "add_builtin_tool_provider", service_mock)

        payload = {"credentials": {"api_key": "sk-test"}, "type": "api-key"}
        with app.test_request_context("/tool-provider/builtin/slack/add", method="POST", json=payload):
            api = tool_providers_module.ToolBuiltinProviderAddApi()
            with pytest.raises(Forbidden):
                api.post(tenant_id="tenant-123", user=_make_account(TenantAccountRole.NORMAL), provider="slack")

        service_mock.assert_not_called()

    def test_owner_add_succeeds(self, app: Flask, tool_providers_module: ModuleType, monkeypatch: pytest.MonkeyPatch):
        owner = _prepare_context(monkeypatch, TenantAccountRole.OWNER)
        service_mock = MagicMock(return_value={"result": "success"})
        monkeypatch.setattr(tool_providers_module.BuiltinToolManageService, "add_builtin_tool_provider", service_mock)

        payload = {"credentials": {"api_key": "sk-test"}, "type": "api-key"}
        with app.test_request_context("/tool-provider/builtin/slack/add", method="POST", json=payload):
            api = tool_providers_module.ToolBuiltinProviderAddApi()
            result = api.post(tenant_id="tenant-123", user=owner, provider="slack")

        assert result["result"] == "success"
        service_mock.assert_called_once()


def _mcp_create_payload() -> dict[str, object]:
    return {
        "server_url": "http://198.51.100.1:9999/sse",
        "name": "niro-vector-mcp-test",
        "icon": "🧪",
        "icon_type": "emoji",
        "icon_background": "#FFFFFF",
        "server_identifier": "niro-vector-mcp-test",
    }


class TestToolProviderMCPApiAuthorization:
    """TC-2981828D: only an owner/admin may create/update/delete workspace-wide MCP providers."""

    def _stub_create(self, tool_providers_module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> MagicMock:
        service = MagicMock()
        service.create_provider.return_value = MagicMock(id="provider-1")
        service.get_provider.return_value = MagicMock(id="provider-1")
        monkeypatch.setattr(tool_providers_module, "MCPToolManageService", MagicMock(return_value=service))
        monkeypatch.setattr(tool_providers_module, "_dump_tool_provider_payload", lambda payload: {"id": "provider-1"})
        return service

    def _stub_update(self, tool_providers_module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> MagicMock:
        service = MagicMock()
        service.get_provider_for_url_validation.return_value = MagicMock()
        service.get_provider.return_value = MagicMock(identity_mode="off")
        monkeypatch.setattr(tool_providers_module, "MCPToolManageService", MagicMock(return_value=service))
        monkeypatch.setattr(
            tool_providers_module.MCPToolManageService, "validate_server_url_standalone", MagicMock(return_value=None)
        )
        monkeypatch.setattr(tool_providers_module, "sessionmaker", lambda *_a, **_k: MagicMock())
        monkeypatch.setattr(tool_providers_module, "db", MagicMock())
        return service

    def _stub_delete(self, tool_providers_module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> MagicMock:
        service = MagicMock()
        monkeypatch.setattr(tool_providers_module, "MCPToolManageService", MagicMock(return_value=service))
        monkeypatch.setattr(tool_providers_module, "sessionmaker", lambda *_a, **_k: MagicMock())
        monkeypatch.setattr(tool_providers_module, "db", MagicMock())
        return service

    def test_normal_member_create_is_rejected(
        self, app: Flask, tool_providers_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ):
        _prepare_context(monkeypatch, TenantAccountRole.NORMAL)
        # Stub the same happy-path service calls the owner test relies on, so
        # an unfixed handler would actually succeed here instead of raising
        # some unrelated error -- isolating the assertion to the missing
        # owner/admin gate itself.
        service = self._stub_create(tool_providers_module, monkeypatch)

        with app.test_request_context("/tool-provider/mcp", method="POST", json=_mcp_create_payload()):
            api = tool_providers_module.ToolProviderMCPApi()
            with pytest.raises(Forbidden):
                api.post(tenant_id="tenant-123", user=_make_account(TenantAccountRole.NORMAL))

        service.create_provider.assert_not_called()

    def test_owner_create_succeeds(
        self, app: Flask, tool_providers_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ):
        owner = _prepare_context(monkeypatch, TenantAccountRole.OWNER)
        service = self._stub_create(tool_providers_module, monkeypatch)

        with app.test_request_context("/tool-provider/mcp", method="POST", json=_mcp_create_payload()):
            api = tool_providers_module.ToolProviderMCPApi()
            result = api.post(tenant_id="tenant-123", user=owner)

        assert result == {"id": "provider-1"}
        service.create_provider.assert_called_once()

    def test_normal_member_update_is_rejected(
        self, app: Flask, tool_providers_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ):
        _prepare_context(monkeypatch, TenantAccountRole.NORMAL)
        service = self._stub_update(tool_providers_module, monkeypatch)

        payload = {**_mcp_create_payload(), "provider_id": "provider-1"}
        with app.test_request_context("/tool-provider/mcp", method="PUT", json=payload):
            api = tool_providers_module.ToolProviderMCPApi()
            with pytest.raises(Forbidden):
                api.put(current_tenant_id="tenant-123")

        service.update_provider.assert_not_called()

    def test_owner_update_succeeds(
        self, app: Flask, tool_providers_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ):
        _prepare_context(monkeypatch, TenantAccountRole.OWNER)
        service = self._stub_update(tool_providers_module, monkeypatch)

        payload = {**_mcp_create_payload(), "provider_id": "provider-1"}
        with app.test_request_context("/tool-provider/mcp", method="PUT", json=payload):
            api = tool_providers_module.ToolProviderMCPApi()
            result = api.put(current_tenant_id="tenant-123")

        assert result["result"] == "success"
        service.update_provider.assert_called_once()

    def test_normal_member_delete_is_rejected(
        self, app: Flask, tool_providers_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ):
        _prepare_context(monkeypatch, TenantAccountRole.NORMAL)
        service = self._stub_delete(tool_providers_module, monkeypatch)

        with app.test_request_context("/tool-provider/mcp", method="DELETE", json={"provider_id": "provider-1"}):
            api = tool_providers_module.ToolProviderMCPApi()
            with pytest.raises(Forbidden):
                api.delete(current_tenant_id="tenant-123")

        service.delete_provider.assert_not_called()

    def test_owner_delete_succeeds(
        self, app: Flask, tool_providers_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ):
        _prepare_context(monkeypatch, TenantAccountRole.OWNER)
        service = self._stub_delete(tool_providers_module, monkeypatch)

        with app.test_request_context("/tool-provider/mcp", method="DELETE", json={"provider_id": "provider-1"}):
            api = tool_providers_module.ToolProviderMCPApi()
            result = api.delete(current_tenant_id="tenant-123")

        assert result["result"] == "success"
        service.delete_provider.assert_called_once_with(tenant_id="tenant-123", provider_id="provider-1")

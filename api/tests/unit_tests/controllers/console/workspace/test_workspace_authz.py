"""Authorization regression tests for owner/admin-only workspace-configuration endpoints.

Covers TC-31119609: `WorkspaceInfoApi.post` (rename workspace) and
`CustomConfigWorkspaceApi.post` (write shared branding config) had no owner/admin
gate at all -- any authenticated normal-role tenant member could rename the
shared workspace or persist branding config visible to every member. The fix
adds `is_admin_or_owner_required` (+ `rbac_permission_required` for the
enterprise RBAC path) to both handlers, mirroring the gate already used on
other owner/admin-only workspace-configuration endpoints in this codebase.

These tests reimport the controller module fresh with only the
transport/plumbing decorators (`login_required`, `setup_required`,
`account_initialization_required`, `with_current_tenant_id`, `with_session`,
the billing-resource-limit check) stubbed to identity, while keeping
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

MODULE_NAME = "controllers.console.workspace.workspace"


@pytest.fixture
def app() -> Flask:
    flask_app = Flask(__name__)
    flask_app.config["TESTING"] = True
    return flask_app


def _identity(func):
    return func


@pytest.fixture
def workspace_module(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Reimport the controller with plumbing decorators neutralized to identity.

    `is_admin_or_owner_required` and `rbac_permission_required` are left real.
    """
    from controllers.common import session as session_module
    from controllers.console import console_ns, wraps
    from libs import login

    monkeypatch.setattr(login, "login_required", _identity)
    monkeypatch.setattr(wraps, "setup_required", _identity)
    monkeypatch.setattr(wraps, "account_initialization_required", _identity)
    monkeypatch.setattr(wraps, "with_current_tenant_id", _identity)
    monkeypatch.setattr(wraps, "with_current_user", _identity)
    monkeypatch.setattr(wraps, "cloud_edition_billing_resource_check", lambda *_a, **_k: _identity)
    monkeypatch.setattr(session_module, "with_session", lambda view=None, **_k: view if view is not None else _identity)

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


class TestWorkspaceInfoApiAuthorization:
    def test_normal_member_rename_is_rejected(
        self, app: Flask, workspace_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ):
        _prepare_context(monkeypatch, TenantAccountRole.NORMAL)
        tenant = MagicMock()
        session = MagicMock()
        # Stub the same happy-path service calls the owner test relies on, so
        # an unfixed handler would actually succeed here instead of raising
        # some unrelated error -- isolating the assertion to the missing
        # owner/admin gate itself.
        monkeypatch.setattr(workspace_module.TenantService, "get_tenant_by_id", lambda *_a, **_k: tenant)
        monkeypatch.setattr(
            workspace_module.WorkspaceService,
            "get_tenant_info",
            lambda *_a, **_k: {"id": "tenant-123", "name": "PWNED-BY-MEMBER"},
        )

        with app.test_request_context("/workspaces/info", json={"name": "PWNED-BY-MEMBER"}):
            api = workspace_module.WorkspaceInfoApi()
            with pytest.raises(Forbidden):
                api.post(session=session, current_tenant_id="tenant-123")

        # The invariant is authorization, not persistence -- but as a sanity
        # check, a rejected request must never reach the write path.
        session.commit.assert_not_called()

    def test_owner_rename_succeeds(self, app: Flask, workspace_module: ModuleType, monkeypatch: pytest.MonkeyPatch):
        _prepare_context(monkeypatch, TenantAccountRole.OWNER)
        tenant = MagicMock()
        session = MagicMock()
        monkeypatch.setattr(workspace_module.TenantService, "get_tenant_by_id", lambda *_a, **_k: tenant)
        monkeypatch.setattr(
            workspace_module.WorkspaceService,
            "get_tenant_info",
            lambda *_a, **_k: {"id": "tenant-123", "name": "New Name"},
        )

        with app.test_request_context("/workspaces/info", json={"name": "New Name"}):
            api = workspace_module.WorkspaceInfoApi()
            result = api.post(session=session, current_tenant_id="tenant-123")

        assert result["result"] == "success"
        assert tenant.name == "New Name"
        session.commit.assert_called_once()


class TestCustomConfigWorkspaceApiAuthorization:
    def test_normal_member_write_is_rejected(
        self, app: Flask, workspace_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ):
        _prepare_context(monkeypatch, TenantAccountRole.NORMAL)
        tenant = MagicMock()
        tenant.custom_config_dict = {}
        session = MagicMock()
        monkeypatch.setattr(workspace_module.TenantService, "get_tenant_by_id", lambda *_a, **_k: tenant)
        monkeypatch.setattr(
            workspace_module.WorkspaceService,
            "get_tenant_info",
            lambda *_a, **_k: {"id": "tenant-123"},
        )

        payload = {"remove_webapp_brand": True, "replace_webapp_logo": "http://evil.example/logo.png"}
        with app.test_request_context("/workspaces/custom-config", json=payload):
            api = workspace_module.CustomConfigWorkspaceApi()
            with pytest.raises(Forbidden):
                api.post(session=session, current_tenant_id="tenant-123")

        session.commit.assert_not_called()

    def test_owner_write_succeeds(self, app: Flask, workspace_module: ModuleType, monkeypatch: pytest.MonkeyPatch):
        _prepare_context(monkeypatch, TenantAccountRole.OWNER)
        tenant = MagicMock()
        tenant.custom_config_dict = {}
        session = MagicMock()
        monkeypatch.setattr(workspace_module.TenantService, "get_tenant_by_id", lambda *_a, **_k: tenant)
        monkeypatch.setattr(
            workspace_module.WorkspaceService,
            "get_tenant_info",
            lambda *_a, **_k: {"id": "tenant-123"},
        )

        payload = {"remove_webapp_brand": True}
        with app.test_request_context("/workspaces/custom-config", json=payload):
            api = workspace_module.CustomConfigWorkspaceApi()
            result = api.post(session=session, current_tenant_id="tenant-123")

        assert result["result"] == "success"
        session.commit.assert_called_once()

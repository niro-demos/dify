"""Authorization regression tests for owner/admin-only model-provider endpoints.

Covers TC-2EEB598D: `ModelProviderModelEnableApi.patch` and
`ModelProviderModelDisableApi.patch` were missing `is_admin_or_owner_required`,
the decorator every sibling model-provider mutation endpoint in this module
carries (`DefaultModelApi.post`, `ModelProviderModelApi.post`/`delete`,
`ModelProviderModelCredentialApi.post`/`put`/`delete`,
`ModelProviderModelCredentialSwitchApi.post`). `rbac_permission_required` alone
is a documented no-op when `RBAC_ENABLED` is `False` (the self-hosted
default), so any authenticated normal-role member could enable/disable models
workspace-wide.

These tests reimport the controller module fresh with only the
transport/plumbing decorators (`login_required`, `setup_required`,
`account_initialization_required`, `with_current_tenant_id`) stubbed to
identity, while keeping `is_admin_or_owner_required` and
`rbac_permission_required` real -- so the assertions exercise the actual
authorization gate rather than a mock of it.
"""

from __future__ import annotations

import importlib
import sys
from types import ModuleType
from unittest.mock import MagicMock

import pytest
from flask import Flask
from werkzeug.exceptions import Forbidden

from graphon.model_runtime.entities.model_entities import ModelType
from models.account import Account, TenantAccountRole

MODULE_NAME = "controllers.console.workspace.models"


@pytest.fixture
def app() -> Flask:
    flask_app = Flask(__name__)
    flask_app.config["TESTING"] = True
    return flask_app


def _identity(func):
    return func


@pytest.fixture
def models_module(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Reimport the controller with plumbing decorators neutralized to identity.

    `is_admin_or_owner_required` and `rbac_permission_required` are left real.
    """
    from controllers.console import console_ns, wraps
    from libs import login

    monkeypatch.setattr(login, "login_required", _identity)
    monkeypatch.setattr(wraps, "setup_required", _identity)
    monkeypatch.setattr(wraps, "account_initialization_required", _identity)
    monkeypatch.setattr(wraps, "with_current_tenant_id", _identity)

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


def _payload() -> dict[str, object]:
    return {"model": "gpt-4", "model_type": ModelType.LLM}


class TestModelProviderModelEnableApiAuthorization:
    def test_normal_member_enable_is_rejected(
        self, app: Flask, models_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ):
        _prepare_context(monkeypatch, TenantAccountRole.NORMAL)
        service_mock = MagicMock()
        monkeypatch.setattr(models_module, "ModelProviderService", lambda: service_mock)

        with app.test_request_context(
            "/workspaces/current/model-providers/openai/models/enable", method="PATCH", json=_payload()
        ):
            api = models_module.ModelProviderModelEnableApi()
            with pytest.raises(Forbidden):
                api.patch(tenant_id="tenant-123", provider="openai")

        service_mock.enable_model.assert_not_called()

    def test_owner_enable_succeeds(self, app: Flask, models_module: ModuleType, monkeypatch: pytest.MonkeyPatch):
        _prepare_context(monkeypatch, TenantAccountRole.OWNER)
        service_mock = MagicMock()
        monkeypatch.setattr(models_module, "ModelProviderService", lambda: service_mock)

        with app.test_request_context(
            "/workspaces/current/model-providers/openai/models/enable", method="PATCH", json=_payload()
        ):
            api = models_module.ModelProviderModelEnableApi()
            result = api.patch(tenant_id="tenant-123", provider="openai")

        assert result["result"] == "success"
        service_mock.enable_model.assert_called_once_with(
            tenant_id="tenant-123", provider="openai", model="gpt-4", model_type=ModelType.LLM
        )


class TestModelProviderModelDisableApiAuthorization:
    def test_normal_member_disable_is_rejected(
        self, app: Flask, models_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ):
        _prepare_context(monkeypatch, TenantAccountRole.NORMAL)
        service_mock = MagicMock()
        monkeypatch.setattr(models_module, "ModelProviderService", lambda: service_mock)

        with app.test_request_context(
            "/workspaces/current/model-providers/openai/models/disable", method="PATCH", json=_payload()
        ):
            api = models_module.ModelProviderModelDisableApi()
            with pytest.raises(Forbidden):
                api.patch(tenant_id="tenant-123", provider="openai")

        service_mock.disable_model.assert_not_called()

    def test_owner_disable_succeeds(self, app: Flask, models_module: ModuleType, monkeypatch: pytest.MonkeyPatch):
        _prepare_context(monkeypatch, TenantAccountRole.OWNER)
        service_mock = MagicMock()
        monkeypatch.setattr(models_module, "ModelProviderService", lambda: service_mock)

        with app.test_request_context(
            "/workspaces/current/model-providers/openai/models/disable", method="PATCH", json=_payload()
        ):
            api = models_module.ModelProviderModelDisableApi()
            result = api.patch(tenant_id="tenant-123", provider="openai")

        assert result["result"] == "success"
        service_mock.disable_model.assert_called_once_with(
            tenant_id="tenant-123", provider="openai", model="gpt-4", model_type=ModelType.LLM
        )

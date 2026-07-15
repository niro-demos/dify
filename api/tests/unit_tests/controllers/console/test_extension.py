from __future__ import annotations

import builtins
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock

import pytest
from flask import Flask
from flask.views import MethodView as FlaskMethodView
from werkzeug.exceptions import Forbidden

_NEEDS_METHOD_VIEW_CLEANUP = False
if not hasattr(builtins, "MethodView"):
    builtins.__dict__["MethodView"] = FlaskMethodView
    _NEEDS_METHOD_VIEW_CLEANUP = True

from constants import HIDDEN_VALUE
from controllers.console.extension import (
    APIBasedExtensionAPI,
    APIBasedExtensionDetailAPI,
    CodeBasedExtensionAPI,
)

if _NEEDS_METHOD_VIEW_CLEANUP:
    del builtins.__dict__["MethodView"]
from models import Account
from models.account import AccountStatus, TenantAccountRole
from models.api_based_extension import APIBasedExtension


def _make_extension(
    *,
    name: str = "Sample Extension",
    api_endpoint: str = "https://example.com/api",
    api_key: str = "super-secret-key",
) -> APIBasedExtension:
    extension = APIBasedExtension(
        tenant_id="tenant-123",
        name=name,
        api_endpoint=api_endpoint,
        api_key=api_key,
    )
    extension.id = f"{uuid.uuid4()}"
    extension.created_at = datetime.now(tz=UTC)
    return extension


def _masked_api_key(api_key: str) -> str:
    if len(api_key) <= 8:
        return api_key[0] + "******" + api_key[-1]
    return api_key[:3] + "******" + api_key[-3:]


def _make_account(
    role: TenantAccountRole,
    *,
    account_id: str = "account-123",
    tenant_id: str = "tenant-123",
) -> Account:
    """Build a real ``Account`` (not a ``MagicMock``) so role-gated decorators such as
    ``is_admin_or_owner_required`` and ``rbac_permission_required`` -- which do a genuine
    ``isinstance(user, Account)`` check and read ``user.is_admin_or_owner`` -- exercise
    their real logic instead of vacuously passing on a mock.
    """
    account = Account(name="Test User", email=f"{account_id}@example.com", status=AccountStatus.ACTIVE)
    account.id = account_id
    account.role = role
    account._current_tenant = SimpleNamespace(id=tenant_id)  # avoids a DB-backed tenant lookup via the real setter
    return account


def _login_as(monkeypatch: pytest.MonkeyPatch, role: TenantAccountRole) -> Account:
    """Swap the logged-in account for the duration of a test."""
    account = _make_account(role)
    monkeypatch.setattr("libs.login._get_user", lambda: account)
    return account


@pytest.fixture(autouse=True)
def _mock_console_guards(monkeypatch: pytest.MonkeyPatch) -> Account:
    """Bypass setup/login plumbing so handlers can run in isolation, logged in as a
    workspace owner by default. Individual tests can call ``_login_as`` to exercise
    the permission gate as a lower-privileged member.
    """

    from controllers.console import wraps as wraps_module

    account = _make_account(TenantAccountRole.OWNER)

    monkeypatch.setattr(wraps_module.dify_config, "EDITION", "CLOUD")
    monkeypatch.setattr(wraps_module.dify_config, "RBAC_ENABLED", False)
    monkeypatch.setattr("libs.login.dify_config.LOGIN_DISABLED", True)
    monkeypatch.delenv("INIT_PASSWORD", raising=False)

    # Route every current-user lookup (login_required, with_current_tenant_id,
    # is_admin_or_owner_required, rbac_permission_required, ...) through the same
    # real account so each decorator's own logic runs for real.
    monkeypatch.setattr("libs.login._get_user", lambda: account)
    monkeypatch.setattr("libs.login.check_csrf_token", lambda *_, **__: None)

    return account


@pytest.fixture(autouse=True)
def _restx_mask_defaults(app: Flask):
    app.config.setdefault("RESTX_MASK_HEADER", "X-Fields")
    app.config.setdefault("RESTX_MASK_SWAGGER", False)


def test_code_based_extension_get_returns_service_data(app: Flask, monkeypatch: pytest.MonkeyPatch):
    service_result = [{"entrypoint": "main:agent"}]
    service_mock = MagicMock(return_value=service_result)
    monkeypatch.setattr(
        "controllers.console.extension.CodeBasedExtensionService.get_code_based_extension",
        service_mock,
    )

    with app.test_request_context(
        "/console/api/code-based-extension",
        method="GET",
        query_string={"module": "workflow.tools"},
    ):
        response = CodeBasedExtensionAPI().get()

    assert response == {"module": "workflow.tools", "data": service_result}
    service_mock.assert_called_once_with("workflow.tools")


def test_api_based_extension_get_returns_tenant_extensions(app: Flask, monkeypatch: pytest.MonkeyPatch):
    extension = _make_extension(name="Weather API", api_key="abcdefghi123")
    service_mock = MagicMock(return_value=[extension])
    monkeypatch.setattr(
        "controllers.console.extension.APIBasedExtensionService.get_all_by_tenant_id",
        service_mock,
    )

    with app.test_request_context("/console/api/api-based-extension", method="GET"):
        response = APIBasedExtensionAPI().get()

    assert response[0]["id"] == extension.id
    assert response[0]["name"] == "Weather API"
    assert response[0]["api_endpoint"] == extension.api_endpoint
    assert response[0]["api_key"].startswith(extension.api_key[:3])
    service_mock.assert_called_once_with("tenant-123", session=ANY)


def test_api_based_extension_post_creates_extension(app: Flask, monkeypatch: pytest.MonkeyPatch):
    saved_extension = _make_extension(name="Docs API", api_key="encrypted-token-from-save")
    save_mock = MagicMock(return_value=saved_extension)
    monkeypatch.setattr("controllers.console.extension.APIBasedExtensionService.save", save_mock)

    payload = {
        "name": "Docs API",
        "api_endpoint": "https://docs.example.com/hook",
        "api_key": "plain-secret",
    }

    with app.test_request_context("/console/api/api-based-extension", method="POST", json=payload):
        response, status = APIBasedExtensionAPI().post()

    args, _ = save_mock.call_args
    created_extension: APIBasedExtension = args[0]
    assert created_extension.tenant_id == "tenant-123"
    assert created_extension.name == payload["name"]
    assert created_extension.api_endpoint == payload["api_endpoint"]
    assert created_extension.api_key == payload["api_key"]
    assert status == 201
    assert response["name"] == saved_extension.name
    assert response["api_key"] == _masked_api_key(payload["api_key"])
    save_mock.assert_called_once()


def test_api_based_extension_detail_get_fetches_extension(app: Flask, monkeypatch: pytest.MonkeyPatch):
    extension = _make_extension(name="Docs API", api_key="abcdefg12345")
    service_mock = MagicMock(return_value=extension)
    monkeypatch.setattr(
        "controllers.console.extension.APIBasedExtensionService.get_with_tenant_id",
        service_mock,
    )

    extension_id = uuid.uuid4()
    with app.test_request_context(f"/console/api/api-based-extension/{extension_id}", method="GET"):
        response = APIBasedExtensionDetailAPI().get(extension_id)

    assert response["id"] == extension.id
    assert response["name"] == extension.name
    service_mock.assert_called_once_with("tenant-123", str(extension_id), session=ANY)


def test_api_based_extension_detail_post_keeps_hidden_api_key(app: Flask, monkeypatch: pytest.MonkeyPatch):
    existing_extension = _make_extension(name="Docs API", api_key="keep-me")
    get_mock = MagicMock(return_value=existing_extension)
    save_mock = MagicMock(return_value=existing_extension)
    monkeypatch.setattr(
        "controllers.console.extension.APIBasedExtensionService.get_with_tenant_id",
        get_mock,
    )
    monkeypatch.setattr("controllers.console.extension.APIBasedExtensionService.save", save_mock)

    payload = {
        "name": "Docs API Updated",
        "api_endpoint": "https://docs.example.com/v2",
        "api_key": HIDDEN_VALUE,
    }

    extension_id = uuid.uuid4()
    with app.test_request_context(
        f"/console/api/api-based-extension/{extension_id}",
        method="POST",
        json=payload,
    ):
        response = APIBasedExtensionDetailAPI().post(extension_id)

    assert existing_extension.name == payload["name"]
    assert existing_extension.api_endpoint == payload["api_endpoint"]
    assert existing_extension.api_key == "keep-me"
    save_mock.assert_called_once_with(existing_extension, session=ANY)
    assert response["name"] == payload["name"]
    assert response["api_key"] == _masked_api_key("keep-me")


def test_api_based_extension_detail_post_updates_api_key_when_provided(app: Flask, monkeypatch: pytest.MonkeyPatch):
    existing_extension = _make_extension(name="Docs API", api_key="old-secret")
    get_mock = MagicMock(return_value=existing_extension)
    save_mock = MagicMock(return_value=existing_extension)
    monkeypatch.setattr(
        "controllers.console.extension.APIBasedExtensionService.get_with_tenant_id",
        get_mock,
    )
    monkeypatch.setattr("controllers.console.extension.APIBasedExtensionService.save", save_mock)

    payload = {
        "name": "Docs API Updated",
        "api_endpoint": "https://docs.example.com/v2",
        "api_key": "new-secret",
    }

    extension_id = uuid.uuid4()
    with app.test_request_context(
        f"/console/api/api-based-extension/{extension_id}",
        method="POST",
        json=payload,
    ):
        response = APIBasedExtensionDetailAPI().post(extension_id)

    assert existing_extension.api_key == "new-secret"
    save_mock.assert_called_once_with(existing_extension, session=ANY)
    assert response["name"] == payload["name"]
    assert response["api_key"] == _masked_api_key(payload["api_key"])


def test_api_based_extension_detail_delete_removes_extension(app: Flask, monkeypatch: pytest.MonkeyPatch):
    existing_extension = _make_extension()
    get_mock = MagicMock(return_value=existing_extension)
    delete_mock = MagicMock()
    monkeypatch.setattr(
        "controllers.console.extension.APIBasedExtensionService.get_with_tenant_id",
        get_mock,
    )
    monkeypatch.setattr("controllers.console.extension.APIBasedExtensionService.delete", delete_mock)

    extension_id = uuid.uuid4()
    with app.test_request_context(
        f"/console/api/api-based-extension/{extension_id}",
        method="DELETE",
    ):
        response, status = APIBasedExtensionDetailAPI().delete(extension_id)

    delete_mock.assert_called_once_with(existing_extension, session=ANY)
    assert status == 204
    assert response == ""


# --- Regression coverage for TC-4F3A5F52 -----------------------------------
#
# Invariant: a workspace member with the plain "normal" role (no app/dataset
# management permission) must not be able to list, create, view, update, or
# delete the tenant's API-based extension integrations (server-side endpoint +
# secret key the backend calls on the tenant's behalf). Every method on both
# resource classes previously carried no permission gate at all.


def test_api_based_extension_get_rejects_normal_member(app: Flask, monkeypatch: pytest.MonkeyPatch):
    _login_as(monkeypatch, TenantAccountRole.NORMAL)
    service_mock = MagicMock(return_value=[])
    monkeypatch.setattr(
        "controllers.console.extension.APIBasedExtensionService.get_all_by_tenant_id",
        service_mock,
    )

    with app.test_request_context("/console/api/api-based-extension", method="GET"):
        with pytest.raises(Forbidden):
            APIBasedExtensionAPI().get()

    service_mock.assert_not_called()


def test_api_based_extension_post_rejects_normal_member(app: Flask, monkeypatch: pytest.MonkeyPatch):
    _login_as(monkeypatch, TenantAccountRole.NORMAL)
    save_mock = MagicMock(return_value=_make_extension())
    monkeypatch.setattr("controllers.console.extension.APIBasedExtensionService.save", save_mock)

    payload = {
        "name": "member-created-ext",
        "api_endpoint": "http://example.com/",
        "api_key": "keyvalue12345",
    }

    with app.test_request_context("/console/api/api-based-extension", method="POST", json=payload):
        with pytest.raises(Forbidden):
            APIBasedExtensionAPI().post()

    save_mock.assert_not_called()


def test_api_based_extension_detail_get_rejects_normal_member(app: Flask, monkeypatch: pytest.MonkeyPatch):
    _login_as(monkeypatch, TenantAccountRole.NORMAL)
    service_mock = MagicMock(return_value=_make_extension())
    monkeypatch.setattr(
        "controllers.console.extension.APIBasedExtensionService.get_with_tenant_id",
        service_mock,
    )

    extension_id = uuid.uuid4()
    with app.test_request_context(f"/console/api/api-based-extension/{extension_id}", method="GET"):
        with pytest.raises(Forbidden):
            APIBasedExtensionDetailAPI().get(extension_id)

    service_mock.assert_not_called()


def test_api_based_extension_detail_post_rejects_normal_member(app: Flask, monkeypatch: pytest.MonkeyPatch):
    _login_as(monkeypatch, TenantAccountRole.NORMAL)
    get_mock = MagicMock(return_value=_make_extension())
    save_mock = MagicMock()
    monkeypatch.setattr(
        "controllers.console.extension.APIBasedExtensionService.get_with_tenant_id",
        get_mock,
    )
    monkeypatch.setattr("controllers.console.extension.APIBasedExtensionService.save", save_mock)

    payload = {
        "name": "member-updated-ext",
        "api_endpoint": "http://example.com/",
        "api_key": "new-secret",
    }

    extension_id = uuid.uuid4()
    with app.test_request_context(
        f"/console/api/api-based-extension/{extension_id}",
        method="POST",
        json=payload,
    ):
        with pytest.raises(Forbidden):
            APIBasedExtensionDetailAPI().post(extension_id)

    get_mock.assert_not_called()
    save_mock.assert_not_called()


def test_api_based_extension_detail_delete_rejects_normal_member(app: Flask, monkeypatch: pytest.MonkeyPatch):
    _login_as(monkeypatch, TenantAccountRole.NORMAL)
    get_mock = MagicMock(return_value=_make_extension())
    delete_mock = MagicMock()
    monkeypatch.setattr(
        "controllers.console.extension.APIBasedExtensionService.get_with_tenant_id",
        get_mock,
    )
    monkeypatch.setattr("controllers.console.extension.APIBasedExtensionService.delete", delete_mock)

    extension_id = uuid.uuid4()
    with app.test_request_context(f"/console/api/api-based-extension/{extension_id}", method="DELETE"):
        with pytest.raises(Forbidden):
            APIBasedExtensionDetailAPI().delete(extension_id)

    get_mock.assert_not_called()
    delete_mock.assert_not_called()


def test_api_based_extension_post_allows_owner(app: Flask, monkeypatch: pytest.MonkeyPatch):
    """Positive control: the same gate must still let a privileged role through."""
    _login_as(monkeypatch, TenantAccountRole.OWNER)
    saved_extension = _make_extension(name="Owner Created", api_key="owner-secret-1")
    save_mock = MagicMock(return_value=saved_extension)
    monkeypatch.setattr("controllers.console.extension.APIBasedExtensionService.save", save_mock)

    payload = {
        "name": "Owner Created",
        "api_endpoint": "https://ops.example.com/hook",
        "api_key": "owner-secret-1",
    }

    with app.test_request_context("/console/api/api-based-extension", method="POST", json=payload):
        response, status = APIBasedExtensionAPI().post()

    assert status == 201
    save_mock.assert_called_once()
    assert response["name"] == "Owner Created"

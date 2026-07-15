from __future__ import annotations

import inspect
import uuid
from collections.abc import Callable
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

import pytest
from werkzeug.exceptions import Forbidden

from controllers.console.apikey import (
    ApiKeyList,
    AppApiKeyListResource,
    BaseApiKeyListResource,
    BaseApiKeyResource,
    DatasetApiKeyListResource,
)
from models import Account
from models.account import AccountStatus, TenantAccountRole
from models.enums import ApiTokenType
from models.model import ApiToken, App


def _make_list_resource() -> BaseApiKeyListResource:
    resource = BaseApiKeyListResource()
    resource.resource_type = ApiTokenType.APP
    resource.resource_model = App
    resource.resource_id_field = "app_id"
    resource.token_prefix = "app-"
    return resource


def _make_key_resource() -> BaseApiKeyResource:
    resource = BaseApiKeyResource()
    resource.resource_type = ApiTokenType.APP
    resource.resource_model = App
    resource.resource_id_field = "app_id"
    return resource


def _make_account(role: TenantAccountRole) -> Account:
    account = Account(
        name="Test User",
        email=f"{role.value}@example.com",
        status=AccountStatus.ACTIVE,
    )
    account.id = f"{role.value}-user"
    account.role = role
    return account


class _CurrentUserProxyStub:
    """Minimal stand-in for flask-login's ``current_user`` LocalProxy.

    ``edit_permission_required`` calls ``current_user._get_current_object()`` directly (rather
    than going through the ``getattr``-guarded fallback other decorators use), so exercising it
    needs a stub that supports that exact call without standing up a full Flask-Login request
    context.
    """

    def __init__(self, account: Account) -> None:
        self._account = account

    def _get_current_object(self) -> Account:
        return self._account

    @property
    def has_edit_permission(self) -> bool:
        return self._account.has_edit_permission


def test_list_api_keys_uses_injected_tenant_id() -> None:
    resource = _make_list_resource()
    api_key = SimpleNamespace(
        id="key-1",
        type=ApiTokenType.APP,
        token="app-token",
        last_used_at=None,
        created_at=None,
    )

    with (
        patch("controllers.console.apikey._get_resource") as get_resource,
        patch("controllers.console.apikey.db") as db_mock,
    ):
        db_mock.session.scalars.return_value.all.return_value = [api_key]

        result = resource.get("app-1", "tenant-1")

    get_resource.assert_called_once_with("app-1", "tenant-1", App)
    assert result == {
        "data": [
            {
                "id": "key-1",
                "type": "app",
                "token": "app-token",
                "last_used_at": None,
                "created_at": None,
            }
        ]
    }


def test_create_api_key_uses_injected_tenant_id() -> None:
    resource = _make_list_resource()
    raw_post = cast(
        Callable[[BaseApiKeyListResource, str, str], tuple[dict[str, object], int]],
        inspect.unwrap(BaseApiKeyListResource.post),
    )

    def add_api_token(api_token: ApiToken) -> None:
        api_token.id = "key-1"

    with (
        patch("controllers.console.apikey._get_resource") as get_resource,
        patch("controllers.console.apikey.db") as db_mock,
        patch("controllers.console.apikey.ApiToken.generate_api_key", return_value="app-generated-token"),
    ):
        db_mock.session.scalar.return_value = 0
        db_mock.session.add.side_effect = add_api_token

        result, status = raw_post(resource, "app-1", "tenant-1")

    get_resource.assert_called_once_with("app-1", "tenant-1", App)
    assert status == 201
    assert result["token"] == "app-generated-token"
    api_token = db_mock.session.add.call_args.args[0]
    assert api_token.app_id == "app-1"
    assert api_token.tenant_id == "tenant-1"
    assert api_token.type == ApiTokenType.APP
    db_mock.session.commit.assert_called_once()


def test_delete_api_key_rejects_non_admin_account() -> None:
    resource = _make_key_resource()

    with (
        patch("controllers.console.apikey._get_resource") as get_resource,
        patch("controllers.console.apikey.db") as db_mock,
    ):
        with pytest.raises(Forbidden):
            resource.delete("app-1", "key-1", "tenant-1", _make_account(TenantAccountRole.NORMAL))

    get_resource.assert_called_once_with("app-1", "tenant-1", App)
    db_mock.session.scalar.assert_not_called()


def test_delete_api_key_uses_injected_user_and_tenant() -> None:
    resource = _make_key_resource()
    api_key = SimpleNamespace(token="app-token", type=ApiTokenType.APP)

    with (
        patch("controllers.console.apikey._get_resource") as get_resource,
        patch("controllers.console.apikey.db") as db_mock,
        patch("controllers.console.apikey.ApiTokenCache.delete") as delete_cache,
    ):
        db_mock.session.scalar.return_value = api_key

        result, status = resource.delete("app-1", "key-1", "tenant-1", _make_account(TenantAccountRole.OWNER))

    get_resource.assert_called_once_with("app-1", "tenant-1", App)
    delete_cache.assert_called_once_with("app-token", ApiTokenType.APP)
    db_mock.session.execute.assert_called_once()
    db_mock.session.commit.assert_called_once()
    assert result == ""
    assert status == 204


@pytest.mark.parametrize(
    "resource_cls",
    [AppApiKeyListResource, DatasetApiKeyListResource],
    ids=["app", "dataset"],
)
def test_get_api_keys_forbidden_for_normal_role_member(
    resource_cls: type[BaseApiKeyListResource],
) -> None:
    """Regression test for TC-3C30075F.

    GET must require the same edit permission POST/DELETE already enforce on this resource, so
    a normal-role (non-editing) tenant member cannot read another member's plaintext API keys.
    The list query itself is patched out so the assertion isolates the permission gate from the
    query implementation.
    """
    resource = resource_cls()
    account = _make_account(TenantAccountRole.NORMAL)

    with (
        patch("controllers.console.wraps.current_account_with_tenant", return_value=(account, "tenant-1")),
        patch("libs.login.current_user", _CurrentUserProxyStub(account)),
        patch.object(resource_cls, "_get_api_key_list") as get_list,
    ):
        with pytest.raises(Forbidden):
            resource.get(resource_id=uuid.uuid4())

    get_list.assert_not_called()


@pytest.mark.parametrize(
    "resource_cls",
    [AppApiKeyListResource, DatasetApiKeyListResource],
    ids=["app", "dataset"],
)
def test_get_api_keys_allowed_for_editing_role_account(
    resource_cls: type[BaseApiKeyListResource],
) -> None:
    """Control for the test above: an editing-role account (the write path's own bar) can still
    list the keys, proving the permission gate discriminates by role rather than blocking GET
    outright."""
    resource = resource_cls()
    account = _make_account(TenantAccountRole.OWNER)
    keys = ApiKeyList(data=[])

    with (
        patch("controllers.console.wraps.current_account_with_tenant", return_value=(account, "tenant-1")),
        patch("libs.login.current_user", _CurrentUserProxyStub(account)),
        patch.object(resource_cls, "_get_api_key_list", return_value=keys) as get_list,
    ):
        result = resource.get(resource_id=uuid.uuid4())

    get_list.assert_called_once()
    assert result == {"data": []}

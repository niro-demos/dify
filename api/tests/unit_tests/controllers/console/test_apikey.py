from __future__ import annotations

import inspect
from collections.abc import Callable
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

import pytest
from werkzeug.exceptions import Forbidden

from controllers.console.apikey import (
    ApiKeyItem,
    ApiKeyList,
    AppApiKeyListResource,
    BaseApiKeyListResource,
    BaseApiKeyResource,
    DatasetApiKeyListResource,
)
from core.rbac import RBACPermission
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


# ---------------------------------------------------------------------------
# Regression: GET /apps/{app_id}/api-keys and GET /datasets/{dataset_id}/api-keys
# must be gated by the same write-level authorization as POST/DELETE.
#
# The `token` field returned by these list endpoints is a live service-API
# secret. A console member with only monitor/read permission on the resource
# must not be able to retrieve it. Before the fix the `get` handler on both
# concrete resources was missing the @edit_permission_required and
# @rbac_permission_required decorators that protect `post` and `delete`, so a
# monitor-only member could call GET and receive the raw token in cleartext.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("resource_cls", "permission", "resource_id"),
    [
        (AppApiKeyListResource, RBACPermission.APP_RELEASE_AND_VERSION, "app-1"),
        (DatasetApiKeyListResource, RBACPermission.DATASET_API_KEY_MANAGE, "dataset-1"),
    ],
)
def test_api_key_list_get_denies_member_without_rbac_permission(
    resource_cls: type[BaseApiKeyListResource],
    permission: RBACPermission,
    resource_id: str,
) -> None:
    """A member RBAC-denied for API-key management must not list API keys.

    Red on the unfixed code: `get` has no @rbac_permission_required, so the
    denied member receives the token and no Forbidden is raised.
    Green after the fix: the decorator raises Forbidden before the token is read.
    """
    account = _make_account(TenantAccountRole.NORMAL)
    resource = resource_cls()

    with (
        patch("controllers.common.wraps.dify_config.RBAC_ENABLED", True),
        patch("controllers.console.wraps.current_account_with_tenant", return_value=(account, "tenant-1")),
        patch("controllers.common.wraps.current_account_with_tenant", return_value=(account, "tenant-1")),
        patch("controllers.common.wraps._extract_resource_id", return_value=resource_id),
        patch("controllers.common.wraps._is_resource_owned_by_current_user", return_value=False),
        patch("controllers.common.wraps.RBACService.CheckAccess.check", return_value=False) as mock_check,
        patch.object(resource, "_get_api_key_list") as mock_list,
    ):
        mock_list.return_value = ApiKeyList(data=[ApiKeyItem(id="key-1", type="app", token="app-leaked-secret")])
        with pytest.raises(Forbidden):
            resource.get(resource_id=resource_id)

    mock_check.assert_called_once()
    assert mock_check.call_args.kwargs["scene"] == permission


@pytest.mark.parametrize(
    ("resource_cls", "permission", "resource_id"),
    [
        (AppApiKeyListResource, RBACPermission.APP_RELEASE_AND_VERSION, "app-1"),
        (DatasetApiKeyListResource, RBACPermission.DATASET_API_KEY_MANAGE, "dataset-1"),
    ],
)
def test_api_key_list_get_allows_member_with_rbac_permission(
    resource_cls: type[BaseApiKeyListResource],
    permission: RBACPermission,
    resource_id: str,
) -> None:
    """Control: a member RBAC-allowed for API-key management can list keys.

    Stays green on both the unfixed and fixed code, proving the deny test
    fails because of the missing permission gate, not a broken setup.
    """
    account = _make_account(TenantAccountRole.OWNER)
    resource = resource_cls()

    with (
        patch("controllers.common.wraps.dify_config.RBAC_ENABLED", True),
        patch("controllers.console.wraps.current_account_with_tenant", return_value=(account, "tenant-1")),
        patch("controllers.common.wraps.current_account_with_tenant", return_value=(account, "tenant-1")),
        patch("controllers.common.wraps._extract_resource_id", return_value=resource_id),
        patch("controllers.common.wraps._is_resource_owned_by_current_user", return_value=False),
        patch("controllers.common.wraps.RBACService.CheckAccess.check", return_value=True),
        patch.object(resource, "_get_api_key_list") as mock_list,
    ):
        mock_list.return_value = ApiKeyList(data=[ApiKeyItem(id="key-1", type="app", token="app-secret")])
        result = resource.get(resource_id=resource_id)

    assert result["data"][0]["token"] == "app-secret"

"""Integration tests for console API key endpoints using testcontainers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from models import Account
from models.account import AccountStatus, TenantAccountRole
from models.enums import ApiTokenType
from models.model import ApiToken, App, AppMode
from tests.test_containers_integration_tests.controllers.console.helpers import (
    authenticate_console_client,
    create_console_account_and_tenant,
    create_console_app,
    create_console_member,
)


@pytest.fixture
def setup_app(
    db_session_with_containers: Session,
    test_client_with_containers: FlaskClient,
) -> tuple[FlaskClient, dict[str, str], App]:
    """Create an authenticated client with an app for API key tests."""
    account, tenant = create_console_account_and_tenant(db_session_with_containers)
    app = create_console_app(db_session_with_containers, tenant.id, account.id, AppMode.CHAT)
    headers = authenticate_console_client(test_client_with_containers, account)
    return test_client_with_containers, headers, app


@pytest.fixture(autouse=True)
def cleanup_api_tokens(db_session_with_containers: Session):
    """Remove API tokens created during each test."""
    yield
    db_session_with_containers.execute(delete(ApiToken))
    db_session_with_containers.commit()


class TestAppApiKeyListResource:
    """Tests for GET/POST /apps/<resource_id>/api-keys."""

    def test_get_empty_keys(self, setup_app: tuple[FlaskClient, dict[str, str], App]) -> None:
        client, headers, app = setup_app
        resp = client.get(f"/console/api/apps/{app.id}/api-keys", headers=headers)
        assert resp.status_code == 200
        assert resp.json is not None
        assert resp.json["data"] == []

    def test_create_api_key(self, setup_app: tuple[FlaskClient, dict[str, str], App]) -> None:
        client, headers, app = setup_app
        resp = client.post(f"/console/api/apps/{app.id}/api-keys", headers=headers)
        assert resp.status_code == 201
        data = resp.json
        assert data is not None
        assert data["token"].startswith("app-")
        assert data["id"] is not None

    def test_create_api_key_persists_authenticated_tenant(
        self,
        setup_app: tuple[FlaskClient, dict[str, str], App],
        db_session_with_containers: Session,
    ) -> None:
        client, headers, app = setup_app
        tenant_id = app.tenant_id

        resp = client.post(f"/console/api/apps/{app.id}/api-keys", headers=headers)

        assert resp.status_code == 201
        assert resp.json is not None
        api_token = db_session_with_containers.scalar(select(ApiToken).where(ApiToken.id == resp.json["id"]))
        assert api_token is not None
        assert api_token.tenant_id == tenant_id
        assert api_token.app_id == app.id
        assert api_token.type == ApiTokenType.APP

    def test_get_keys_after_create(self, setup_app: tuple[FlaskClient, dict[str, str], App]) -> None:
        client, headers, app = setup_app
        client.post(f"/console/api/apps/{app.id}/api-keys", headers=headers)
        client.post(f"/console/api/apps/{app.id}/api-keys", headers=headers)

        resp = client.get(f"/console/api/apps/{app.id}/api-keys", headers=headers)
        assert resp.status_code == 200
        assert resp.json is not None
        assert len(resp.json["data"]) == 2

    def test_create_key_max_limit(
        self,
        setup_app: tuple[FlaskClient, dict[str, str], App],
        db_session_with_containers: Session,
    ) -> None:
        client, headers, app = setup_app
        # Create 10 keys (the max)
        for _ in range(10):
            client.post(f"/console/api/apps/{app.id}/api-keys", headers=headers)

        # 11th should fail
        resp = client.post(f"/console/api/apps/{app.id}/api-keys", headers=headers)
        assert resp.status_code == 400

    def test_get_keys_for_nonexistent_app(
        self,
        setup_app: tuple[FlaskClient, dict[str, str], App],
    ) -> None:
        client, headers, _ = setup_app
        resp = client.get(
            "/console/api/apps/00000000-0000-0000-0000-000000000000/api-keys",
            headers=headers,
        )
        assert resp.status_code == 404

    def test_get_foreign_app_keys_not_found(
        self,
        setup_app: tuple[FlaskClient, dict[str, str], App],
        db_session_with_containers: Session,
    ) -> None:
        client, headers, _ = setup_app
        foreign_account, foreign_tenant = create_console_account_and_tenant(db_session_with_containers)
        foreign_app = create_console_app(
            db_session_with_containers, foreign_tenant.id, foreign_account.id, AppMode.CHAT
        )

        resp = client.get(f"/console/api/apps/{foreign_app.id}/api-keys", headers=headers)

        assert resp.status_code == 404

    def test_get_keys_forbidden_for_normal_role_member(
        self,
        db_session_with_containers: Session,
        test_client_with_containers: FlaskClient,
    ) -> None:
        """A normal-role workspace member must not be able to read the app's plaintext API keys.

        Regression test: the GET handler used to be missing the @edit_permission_required gate
        that the sibling POST/DELETE handlers on this same resource already carry, letting any
        tenant member read live Service-API bearer tokens regardless of role.

        This test issues exactly one authenticated HTTP request (the member's GET). The pre-
        existing key is seeded via a direct DB insert instead of an owner HTTP call:
        ``db_session_with_containers`` holds one Flask app context open for the whole test, so
        Flask reuses that context -- and the ``flask.g`` it carries -- for every request made
        through a test client during the test. A second authenticated request would therefore
        silently reuse the first request's resolved ``current_user`` instead of re-authenticating
        as the new actor, rather than re-invoking the login machinery under test. The owner side
        of this invariant (an editing-role account can still list its own keys) is already covered
        by ``test_get_empty_keys`` / ``test_get_keys_after_create`` above, which each make a single
        request as the same actor.
        """
        owner, tenant = create_console_account_and_tenant(db_session_with_containers)
        app = create_console_app(db_session_with_containers, tenant.id, owner.id, AppMode.CHAT)
        db_session_with_containers.add(
            ApiToken(
                app_id=app.id,
                tenant_id=tenant.id,
                type=ApiTokenType.APP,
                token=ApiToken.generate_api_key("app-", 24),
            )
        )
        db_session_with_containers.commit()

        member = create_console_member(db_session_with_containers, tenant.id, role=TenantAccountRole.NORMAL)
        member_headers = authenticate_console_client(test_client_with_containers, member)

        resp = test_client_with_containers.get(f"/console/api/apps/{app.id}/api-keys", headers=member_headers)

        assert resp.status_code == 403


class TestAppApiKeyResource:
    """Tests for DELETE /apps/<resource_id>/api-keys/<api_key_id>."""

    def test_delete_key_success(self, setup_app: tuple[FlaskClient, dict[str, str], App]) -> None:
        client, headers, app = setup_app
        create_resp = client.post(f"/console/api/apps/{app.id}/api-keys", headers=headers)
        assert create_resp.json is not None
        key_id = create_resp.json["id"]

        resp = client.delete(f"/console/api/apps/{app.id}/api-keys/{key_id}", headers=headers)
        assert resp.status_code == 204

    def test_delete_nonexistent_key(self, setup_app: tuple[FlaskClient, dict[str, str], App]) -> None:
        client, headers, app = setup_app
        resp = client.delete(
            f"/console/api/apps/{app.id}/api-keys/00000000-0000-0000-0000-000000000000",
            headers=headers,
        )
        assert resp.status_code == 404

    def test_delete_key_nonexistent_app(
        self,
        setup_app: tuple[FlaskClient, dict[str, str], App],
    ) -> None:
        client, headers, _ = setup_app
        resp = client.delete(
            "/console/api/apps/00000000-0000-0000-0000-000000000000/api-keys/00000000-0000-0000-0000-000000000000",
            headers=headers,
        )
        assert resp.status_code == 404

    def test_delete_forbidden_for_non_admin(
        self,
        flask_app_with_containers: Flask,
    ) -> None:
        """A non-admin member cannot delete API keys via the controller permission check."""
        from werkzeug.exceptions import Forbidden

        from controllers.console.apikey import BaseApiKeyResource

        resource = BaseApiKeyResource()
        resource.resource_type = ApiTokenType.APP
        resource.resource_model = MagicMock()
        resource.resource_id_field = "app_id"

        non_admin = Account(name="Normal User", email="normal@example.com", status=AccountStatus.ACTIVE)
        non_admin.id = "normal-user"
        non_admin.role = TenantAccountRole.NORMAL

        with (
            flask_app_with_containers.test_request_context("/"),
            patch("controllers.console.apikey._get_resource"),
        ):
            with pytest.raises(Forbidden):
                BaseApiKeyResource.delete(resource, "rid", "kid", "tenant-id", non_admin)

    # Note: DatasetApiKeyListResource.get's equivalent permission gate is covered at the unit
    # level (tests/unit_tests/controllers/console/test_apikey.py), not here. Creating or listing
    # dataset API keys through this HTTP path currently 500s regardless of role, because
    # `ApiToken` has no mapped `dataset_id` column despite the `api_tokens` table having one
    # (see the "bug: this uses setattr" comment on `ApiToken` in models/model.py) -- a
    # pre-existing, unrelated defect that is out of scope for this permission fix.

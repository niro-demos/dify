"""Integration tests for console API key endpoints using testcontainers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from models import Account, Site
from models.account import AccountStatus, TenantAccountRole
from models.agent import Agent, AgentScope, AgentSource, AgentStatus
from models.enums import ApiTokenType
from models.model import ApiToken, App, AppMode
from tests.test_containers_integration_tests.controllers.console.helpers import (
    authenticate_console_client,
    create_console_account_and_tenant,
    create_console_app,
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


def _create_normal_member(db_session: Session, owner: Account) -> Account:
    """Create a same-tenant normal member for negative authorization checks."""
    tenant_id = owner.current_tenant_id
    assert tenant_id is not None

    account = Account(
        email=f"normal-{owner.id}@example.com",
        name="Normal User",
        interface_language="en-US",
        status=AccountStatus.ACTIVE,
    )
    account.initialized_at = owner.initialized_at
    db_session.add(account)
    db_session.commit()

    from models import TenantAccountJoin

    db_session.add(
        TenantAccountJoin(
            tenant_id=tenant_id,
            account_id=account.id,
            role=TenantAccountRole.NORMAL,
            current=True,
        )
    )
    db_session.commit()

    account.set_tenant_id(tenant_id)
    account.timezone = "UTC"
    db_session.commit()
    return account


def _create_site(db_session: Session, app: App, account_id: str, code: str) -> Site:
    site = Site(
        app_id=app.id,
        title="Test Site",
        default_language="en-US",
        customize_token_strategy="not_allow",
        prompt_public=False,
        code=code,
        created_by=account_id,
        updated_by=account_id,
    )
    db_session.add(site)
    db_session.commit()
    return site


def _create_agent_for_app(db_session: Session, tenant_id: str, account_id: str, app: App) -> Agent:
    agent = Agent(
        tenant_id=tenant_id,
        name="Test Agent",
        description="",
        role="",
        scope=AgentScope.ROSTER,
        source=AgentSource.AGENT_APP,
        status=AgentStatus.ACTIVE,
        app_id=app.id,
        backing_app_id=app.id,
        created_by=account_id,
        updated_by=account_id,
    )
    db_session.add(agent)
    db_session.commit()
    return agent


@pytest.fixture(autouse=True)
def cleanup_api_tokens(db_session_with_containers: Session):
    """Remove API tokens created during each test."""
    yield
    db_session_with_containers.rollback()
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

    def test_normal_member_cannot_read_peer_app_raw_api_keys(
        self,
        db_session_with_containers: Session,
        test_client_with_containers: FlaskClient,
    ) -> None:
        owner, tenant = create_console_account_and_tenant(db_session_with_containers)
        normal = _create_normal_member(db_session_with_containers, owner)
        app = create_console_app(db_session_with_containers, tenant.id, owner.id, AppMode.CHAT)
        token = ApiToken(
            tenant_id=tenant.id,
            app_id=app.id,
            type=ApiTokenType.APP,
            token="app-regression-secret-token",
        )
        db_session_with_containers.add(token)
        db_session_with_containers.commit()

        headers = authenticate_console_client(test_client_with_containers, normal)

        resp = test_client_with_containers.get(f"/console/api/apps/{app.id}/api-keys", headers=headers)

        assert resp.status_code == 403

    def test_normal_member_cannot_read_agent_service_api_keys(
        self,
        db_session_with_containers: Session,
        test_client_with_containers: FlaskClient,
    ) -> None:
        owner, tenant = create_console_account_and_tenant(db_session_with_containers)
        normal = _create_normal_member(db_session_with_containers, owner)
        app = create_console_app(db_session_with_containers, tenant.id, owner.id, AppMode.AGENT)
        agent = _create_agent_for_app(db_session_with_containers, tenant.id, owner.id, app)
        token = ApiToken(
            tenant_id=tenant.id,
            app_id=app.id,
            type=ApiTokenType.APP,
            token="app-agent-regression-secret-token",
        )
        db_session_with_containers.add(token)
        db_session_with_containers.commit()

        headers = authenticate_console_client(test_client_with_containers, normal)

        resp = test_client_with_containers.get(f"/console/api/agent/{agent.id}/api-keys", headers=headers)

        assert resp.status_code == 403

    def test_normal_member_app_detail_does_not_expose_site_secret(
        self,
        db_session_with_containers: Session,
        test_client_with_containers: FlaskClient,
    ) -> None:
        owner, tenant = create_console_account_and_tenant(db_session_with_containers)
        normal = _create_normal_member(db_session_with_containers, owner)
        app = create_console_app(db_session_with_containers, tenant.id, owner.id, AppMode.CHAT)
        _create_site(db_session_with_containers, app, owner.id, "site-regression-secret")
        headers = authenticate_console_client(test_client_with_containers, normal)

        resp = test_client_with_containers.get(f"/console/api/apps/{app.id}", headers=headers)

        assert resp.status_code == 200
        assert resp.json is not None
        assert resp.json["site"] is not None
        assert resp.json["site"].get("access_token") is None
        assert resp.json["site"].get("code") is None

    def test_normal_member_cannot_read_workspace_dataset_api_keys(
        self,
        db_session_with_containers: Session,
        test_client_with_containers: FlaskClient,
    ) -> None:
        owner, tenant = create_console_account_and_tenant(db_session_with_containers)
        normal = _create_normal_member(db_session_with_containers, owner)
        token = ApiToken(
            tenant_id=tenant.id,
            type=ApiTokenType.DATASET,
            token="dataset-regression-secret-token",
        )
        db_session_with_containers.add(token)
        db_session_with_containers.commit()
        headers = authenticate_console_client(test_client_with_containers, normal)

        resp = test_client_with_containers.get("/console/api/datasets/api-keys", headers=headers)

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


class TestConsolePrivilegedAppSurfaces:
    """Regression coverage for same-tenant normal members reaching privileged app surfaces."""

    def test_normal_member_cannot_invoke_audio_debug_routes(
        self,
        db_session_with_containers: Session,
        test_client_with_containers: FlaskClient,
    ) -> None:
        owner, tenant = create_console_account_and_tenant(db_session_with_containers)
        normal = _create_normal_member(db_session_with_containers, owner)
        app = create_console_app(db_session_with_containers, tenant.id, owner.id, AppMode.CHAT)
        headers = authenticate_console_client(test_client_with_containers, normal)

        audio_resp = test_client_with_containers.post(f"/console/api/apps/{app.id}/audio-to-text", headers=headers)
        tts_resp = test_client_with_containers.post(
            f"/console/api/apps/{app.id}/text-to-audio",
            json={"text": "hello"},
            headers=headers,
        )

        assert audio_resp.status_code == 403
        assert tts_resp.status_code == 403

    @pytest.mark.parametrize(
        ("path_template", "expected_unfixed_status"),
        [
            ("/console/api/apps/{app_id}/workflow-runs", 200),
            ("/console/api/apps/{app_id}/workflow-runs/count", 200),
            ("/console/api/apps/{app_id}/workflow-runs/00000000-0000-0000-0000-000000000000", 404),
            (
                "/console/api/apps/{app_id}/workflow-runs/00000000-0000-0000-0000-000000000000/node-executions",
                404,
            ),
        ],
    )
    def test_normal_member_cannot_read_workflow_run_surfaces(
        self,
        path_template: str,
        expected_unfixed_status: int,
        db_session_with_containers: Session,
        test_client_with_containers: FlaskClient,
    ) -> None:
        owner, tenant = create_console_account_and_tenant(db_session_with_containers)
        normal = _create_normal_member(db_session_with_containers, owner)
        app = create_console_app(db_session_with_containers, tenant.id, owner.id, AppMode.WORKFLOW)
        headers = authenticate_console_client(test_client_with_containers, normal)

        resp = test_client_with_containers.get(path_template.format(app_id=app.id), headers=headers)

        assert resp.status_code == 403, (
            f"unfixed code reaches the handler and returns HTTP {expected_unfixed_status}; "
            "normal members must be denied before workflow run data lookup"
        )

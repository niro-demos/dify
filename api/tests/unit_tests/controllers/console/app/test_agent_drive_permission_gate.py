"""Regression tests for TC-2769AFA5.

Invariant: a workspace member holding only the view-only "normal" role (no
app-edit permission) must be rejected with 403 forbidden by every app-mutating
console endpoint -- exactly like `POST .../workflows/draft` already rejects
that same session (see `controllers/console/app/workflow.py::DraftWorkflowApi.post`).

Before the fix, none of the agent-drive file/skill mutation handlers in
`controllers/console/app/agent.py` and the config-file/config-skill mutation
handlers in `controllers/console/app/agent_config_inspector.py` carried the
`edit_permission_required` / `rbac_permission_required(..., APP_EDIT)` gate, so
a normal-role member's request reached business logic (and failed only with a
handler-internal `400 agent_not_bound` once it got there) instead of being
rejected at the authorization layer.

`_BusinessLogicReachedError` is raised by the mocked agent/app resolution calls
that sit just past where the fix inserts its gate. If a case reaches that
mock, the request demonstrably crossed the authorization boundary -- which is
exactly the defect TC-2769AFA5 describes.
"""

from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from flask import Flask
from werkzeug.exceptions import Forbidden

from controllers.common import session as session_module
from controllers.console import wraps as console_wraps
from controllers.console.app import agent as agent_module
from controllers.console.app import agent_config_inspector as inspector_module
from controllers.console.app import wraps as app_wraps
from libs import login as login_lib
from models.account import Account, AccountStatus, TenantAccountRole


class _BusinessLogicReachedError(Exception):
    """Sentinel: raised by mocks planted just past the authorization gate."""


class _SentinelSession:
    """Session stand-in whose `scalar` (app lookup) proves the gate was skipped."""

    def scalar(self, *_args: object, **_kwargs: object) -> object:
        raise _BusinessLogicReachedError("get_app_model queried the app -- past the authorization layer")

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass


def _make_account(role: TenantAccountRole) -> Account:
    account = Account(name="tester", email="tester@example.com")
    account.status = AccountStatus.ACTIVE
    account.role = role
    account.id = "account-123"  # type: ignore[assignment]
    account._current_tenant = SimpleNamespace(id="tenant-123")  # type: ignore[attr-defined]
    account._get_current_object = lambda: account  # type: ignore[attr-defined]
    return account


def _patch_guards(monkeypatch: pytest.MonkeyPatch, account: Account) -> None:
    """Wire login/tenant/edition plumbing so only the permission gate under test governs the call."""
    monkeypatch.setattr(login_lib.dify_config, "LOGIN_DISABLED", True)
    monkeypatch.setattr(login_lib, "current_user", account)
    monkeypatch.setattr(login_lib, "current_account_with_tenant", lambda: (account, account.current_tenant_id))
    monkeypatch.setattr(login_lib, "check_csrf_token", lambda *_, **__: None)
    monkeypatch.setattr(console_wraps, "current_account_with_tenant", lambda: (account, account.current_tenant_id))
    monkeypatch.setattr(console_wraps.dify_config, "EDITION", "CLOUD")
    monkeypatch.setattr(console_wraps.dify_config, "RBAC_ENABLED", False)
    monkeypatch.setattr(app_wraps, "current_account_with_tenant", lambda: (account, account.current_tenant_id))


def _patch_business_layer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Plant `_BusinessLogicReachedError` at the app/agent resolution calls just past the fix's gate."""
    monkeypatch.setattr(session_module.session_factory, "create_session", lambda: nullcontext(_SentinelSession()))
    reached = MagicMock(
        side_effect=_BusinessLogicReachedError("resolved an agent's app -- past the authorization layer")
    )
    monkeypatch.setattr(agent_module, "resolve_agent_runtime_app_model", reached)
    monkeypatch.setattr(inspector_module, "resolve_agent_runtime_app_model", reached)


# One case per mutating agent-drive / config-file / config-skill route that
# must be gated identically to POST /apps/<app_id>/workflows/draft. `ctx` holds
# the extra `app.test_request_context` kwargs a case needs (only
# AgentConfigFilesByAgentApi.post reads its JSON payload before the fix's gate
# would otherwise stop it).
MUTATION_CASES: list[tuple[type, str, dict[str, str], dict[str, object]]] = [
    (agent_module.AgentSkillUploadByAgentApi, "post", {"agent_id": "agent-1"}, {}),
    (agent_module.AgentSkillUploadApi, "post", {"app_id": "app-1"}, {}),
    (agent_module.AgentDriveFilesByAgentApi, "post", {"agent_id": "agent-1"}, {}),
    (agent_module.AgentDriveFilesByAgentApi, "delete", {"agent_id": "agent-1"}, {}),
    (agent_module.AgentDriveFilesApi, "post", {"app_id": "app-1"}, {}),
    (agent_module.AgentDriveFilesApi, "delete", {"app_id": "app-1"}, {}),
    (agent_module.AgentSkillByAgentApi, "delete", {"agent_id": "agent-1", "slug": "s"}, {}),
    (agent_module.AgentSkillApi, "delete", {"app_id": "app-1", "slug": "s"}, {}),
    (inspector_module.AgentConfigSkillUploadByAgentApi, "post", {"agent_id": "agent-1"}, {}),
    (
        inspector_module.AgentConfigFilesByAgentApi,
        "post",
        {"agent_id": "agent-1"},
        {"json": {"upload_file_id": "0fa6f9bc-3416-4476-8857-a13129704dd9"}},
    ),
    (inspector_module.AgentConfigSkillByAgentApi, "delete", {"agent_id": "agent-1", "name": "n"}, {}),
    (inspector_module.AgentConfigFileByAgentApi, "delete", {"agent_id": "agent-1", "name": "n"}, {}),
]


@pytest.mark.parametrize(
    ("resource_cls", "method_name", "kwargs", "ctx"),
    MUTATION_CASES,
    ids=[f"{cls.__name__}.{method}" for cls, method, _, _ctx in MUTATION_CASES],
)
def test_mutation_endpoints_reject_normal_role(
    app: Flask,
    monkeypatch: pytest.MonkeyPatch,
    resource_cls: type,
    method_name: str,
    kwargs: dict[str, str],
    ctx: dict[str, object],
) -> None:
    """A normal-role (non-edit) member must get 403 and never reach app/agent resolution."""
    account = _make_account(TenantAccountRole.NORMAL)
    _patch_guards(monkeypatch, account)
    _patch_business_layer(monkeypatch)

    with app.test_request_context("/", method=method_name.upper(), **ctx):
        handler = getattr(resource_cls(), method_name)
        with pytest.raises(Forbidden):
            handler(**kwargs)


@pytest.mark.parametrize(
    ("resource_cls", "method_name", "kwargs", "ctx"),
    MUTATION_CASES,
    ids=[f"{cls.__name__}.{method}" for cls, method, _, _ctx in MUTATION_CASES],
)
def test_mutation_endpoints_allow_editor_role(
    app: Flask,
    monkeypatch: pytest.MonkeyPatch,
    resource_cls: type,
    method_name: str,
    kwargs: dict[str, str],
    ctx: dict[str, object],
) -> None:
    """Editor role clears the gate: the call proceeds to (mocked) app/agent resolution, not Forbidden."""
    account = _make_account(TenantAccountRole.EDITOR)
    _patch_guards(monkeypatch, account)
    _patch_business_layer(monkeypatch)

    with app.test_request_context("/", method=method_name.upper(), **ctx):
        handler = getattr(resource_cls(), method_name)
        with pytest.raises(_BusinessLogicReachedError):
            handler(**kwargs)


def _patch_session_factory_with_app(monkeypatch: pytest.MonkeyPatch, app_model: object) -> None:
    class _AppSession:
        def scalar(self, *_args: object, **_kwargs: object) -> object:
            return app_model

        def commit(self) -> None:
            pass

        def rollback(self) -> None:
            pass

    monkeypatch.setattr(session_module.session_factory, "create_session", lambda: nullcontext(_AppSession()))


def test_delete_drive_file_full_success_for_editor(app: Flask, monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end proof for the PoC's DELETE .../agent/files target: editor role reaches AgentDriveService."""
    from models.model import AppMode

    account = _make_account(TenantAccountRole.EDITOR)
    _patch_guards(monkeypatch, account)

    app_model = SimpleNamespace(
        id="app-1",
        tenant_id="tenant-123",
        mode=AppMode.WORKFLOW,
        status="normal",
        bound_agent_id_with_session=lambda *, session: "agent-1",
    )
    _patch_session_factory_with_app(monkeypatch, app_model)

    drive = MagicMock()
    drive.return_value.commit.return_value = [{"key": "files/sample.pdf", "removed": True}]
    monkeypatch.setattr(agent_module, "AgentDriveService", drive)

    with app.test_request_context("/?key=files/sample.pdf", method="DELETE"):
        body = agent_module.AgentDriveFilesApi().delete(app_id="app-1")

    assert body == {"result": "success", "removed_keys": ["files/sample.pdf"]}


def test_upload_skill_full_success_for_editor(app: Flask, monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end proof for the PoC's POST .../agent/skills/upload target: editor role reaches the service."""
    import io

    from models.model import AppMode

    account = _make_account(TenantAccountRole.EDITOR)
    _patch_guards(monkeypatch, account)

    app_model = SimpleNamespace(
        id="app-1",
        tenant_id="tenant-123",
        mode=AppMode.WORKFLOW,
        status="normal",
        bound_agent_id_with_session=lambda *, session: "agent-1",
    )
    _patch_session_factory_with_app(monkeypatch, app_model)

    svc = MagicMock()
    svc.return_value.standardize.return_value = {
        "skill": {"path": "skill-a", "skill_md_key": "skill-a/SKILL.md"},
        "manifest": {"name": "Skill A"},
    }
    monkeypatch.setattr(agent_module, "SkillStandardizeService", svc)

    data = {"file": (io.BytesIO(b"zip-bytes"), "skill.zip")}
    with app.test_request_context("/", method="POST", data=data, content_type="multipart/form-data"):
        body, status = agent_module.AgentSkillUploadApi().post(app_id="app-1")

    assert status == 201
    assert body["skill"] == {"path": "skill-a", "skill_md_key": "skill-a/SKILL.md"}
    assert svc.return_value.standardize.call_args.kwargs["agent_id"] == "agent-1"

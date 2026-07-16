"""Unit tests for controllers.web.workflow endpoints."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

from controllers.web.error import (
    NotWorkflowAppError,
    ProviderNotInitializeError,
    ProviderQuotaExceededError,
)
from controllers.web.workflow import WorkflowRunApi, WorkflowTaskStopApi
from core.app.entities.app_invoke_entities import InvokeFrom
from core.errors.error import ProviderTokenNotInitError, QuotaExceededError
from models.model import AppMode


def _workflow_app() -> SimpleNamespace:
    return SimpleNamespace(id="app-1", mode="workflow")


def _chat_app() -> SimpleNamespace:
    return SimpleNamespace(id="app-1", mode="chat")


def _end_user(user_id: str = "eu-1") -> SimpleNamespace:
    return SimpleNamespace(id=user_id)


# ---------------------------------------------------------------------------
# WorkflowRunApi
# ---------------------------------------------------------------------------
class TestWorkflowRunApi:
    def test_wrong_mode_raises(self, app: Flask) -> None:
        with app.test_request_context("/workflows/run", method="POST"):
            with pytest.raises(NotWorkflowAppError):
                WorkflowRunApi().post(_chat_app(), _end_user())

    @patch("controllers.web.workflow.helper.compact_generate_response", return_value={"result": "ok"})
    @patch("controllers.web.workflow.AppGenerateService.generate")
    @patch("controllers.web.workflow.web_ns")
    def test_happy_path(self, mock_ns: MagicMock, mock_gen: MagicMock, mock_compact: MagicMock, app: Flask) -> None:
        mock_ns.payload = {"inputs": {"key": "val"}}
        mock_gen.return_value = "response"

        with app.test_request_context("/workflows/run", method="POST"):
            result = WorkflowRunApi().post(_workflow_app(), _end_user())

        assert result == {"result": "ok"}

    @patch(
        "controllers.web.workflow.AppGenerateService.generate",
        side_effect=ProviderTokenNotInitError(description="not init"),
    )
    @patch("controllers.web.workflow.web_ns")
    def test_provider_not_init(self, mock_ns: MagicMock, mock_gen: MagicMock, app: Flask) -> None:
        mock_ns.payload = {"inputs": {}}

        with app.test_request_context("/workflows/run", method="POST"):
            with pytest.raises(ProviderNotInitializeError):
                WorkflowRunApi().post(_workflow_app(), _end_user())

    @patch(
        "controllers.web.workflow.AppGenerateService.generate",
        side_effect=QuotaExceededError(),
    )
    @patch("controllers.web.workflow.web_ns")
    def test_quota_exceeded(self, mock_ns: MagicMock, mock_gen: MagicMock, app: Flask) -> None:
        mock_ns.payload = {"inputs": {}}

        with app.test_request_context("/workflows/run", method="POST"):
            with pytest.raises(ProviderQuotaExceededError):
                WorkflowRunApi().post(_workflow_app(), _end_user())


# ---------------------------------------------------------------------------
# WorkflowTaskStopApi
# ---------------------------------------------------------------------------
class TestWorkflowTaskStopApi:
    def test_wrong_mode_raises(self, app: Flask) -> None:
        with app.test_request_context("/workflows/tasks/task-1/stop", method="POST"):
            with pytest.raises(NotWorkflowAppError):
                WorkflowTaskStopApi().post(_chat_app(), _end_user(), "task-1")

    @patch("controllers.web.workflow.AppTaskService.stop_task")
    def test_delegates_to_app_task_service_with_caller_identity(self, mock_stop_task: MagicMock, app: Flask) -> None:
        """The controller must hand the *caller's own* identity down to
        AppTaskService.stop_task -- it's that identity, not the task_id alone,
        that AppTaskService/AppQueueManager use to decide ownership."""
        with app.test_request_context("/workflows/tasks/task-1/stop", method="POST"):
            result = WorkflowTaskStopApi().post(_workflow_app(), _end_user("eu-1"), "task-1")

        assert result == {"result": "success"}
        mock_stop_task.assert_called_once_with(
            task_id="task-1",
            invoke_from=InvokeFrom.WEB_APP,
            user_id="eu-1",
            app_mode=AppMode.WORKFLOW,
        )

    # -------------------------------------------------------------------
    # Regression test for TC-C2330FD6: cross-tenant/cross-user task stop.
    #
    # Invariant: an end user of one published workflow app must not be able
    # to stop/cancel another user's (or another tenant's) in-progress
    # workflow run, no matter how the task_id became known to them.
    #
    # These drive the real ownership check (AppQueueManager.set_stop_flag,
    # via AppTaskService.stop_task) end-to-end from the controller; only the
    # redis client and the external GraphEngine are mocked.
    # -------------------------------------------------------------------
    @patch("services.app_task_service.GraphEngineManager")
    @patch("core.app.apps.base_app_queue_manager.redis_client")
    def test_cannot_stop_a_task_owned_by_another_end_user(
        self, mock_redis: MagicMock, mock_graph_engine_manager: MagicMock, app: Flask
    ) -> None:
        # task-1 is recorded (by AppQueueManager, when the run started) as
        # belonging to end-user "victim-1".
        mock_redis.get.return_value = b"end-user-victim-1"

        # A different end-user ("attacker-2") tries to stop it.
        with app.test_request_context("/workflows/tasks/task-1/stop", method="POST"):
            result = WorkflowTaskStopApi().post(_workflow_app(), _end_user("attacker-2"), "task-1")

        # The response keeps its existing "always success" shape (no
        # information leak about the task's real owner)...
        assert result == {"result": "success"}
        # ...but neither stop mechanism actually touched the victim's task:
        # no stop flag was set, and no stop command reached the graph engine.
        mock_redis.setex.assert_not_called()
        mock_graph_engine_manager.return_value.send_stop_command.assert_not_called()

    @patch("services.app_task_service.GraphEngineManager")
    @patch("core.app.apps.base_app_queue_manager.redis_client")
    def test_owning_end_user_can_still_stop_their_own_task(
        self, mock_redis: MagicMock, mock_graph_engine_manager: MagicMock, app: Flask
    ) -> None:
        # Control: the legitimate owner performing the identical call must
        # still succeed -- proves the rejection above is the ownership check
        # rejecting a different caller, not a broken/over-eager environment.
        mock_redis.get.return_value = b"end-user-victim-1"

        with app.test_request_context("/workflows/tasks/task-1/stop", method="POST"):
            result = WorkflowTaskStopApi().post(_workflow_app(), _end_user("victim-1"), "task-1")

        assert result == {"result": "success"}
        mock_redis.setex.assert_called_once()
        mock_graph_engine_manager.return_value.send_stop_command.assert_called_once_with("task-1")

    @patch("services.app_task_service.GraphEngineManager")
    @patch("core.app.apps.base_app_queue_manager.redis_client")
    def test_cannot_stop_a_task_that_was_never_created(
        self, mock_redis: MagicMock, mock_graph_engine_manager: MagicMock, app: Flask
    ) -> None:
        # No "generate_task_belong:<task_id>" key exists at all for a
        # fabricated task_id -- must be rejected too, not just accepted.
        mock_redis.get.return_value = None

        with app.test_request_context("/workflows/tasks/nonexistent-task/stop", method="POST"):
            result = WorkflowTaskStopApi().post(_workflow_app(), _end_user("attacker-2"), "nonexistent-task")

        assert result == {"result": "success"}
        mock_redis.setex.assert_not_called()
        mock_graph_engine_manager.return_value.send_stop_command.assert_not_called()

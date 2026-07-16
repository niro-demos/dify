"""Service for managing application task operations.

This service provides centralized logic for task control operations
like stopping tasks, handling both legacy Redis flag mechanism and
new GraphEngine command channel mechanism.
"""

from core.app.apps.base_app_queue_manager import AppQueueManager
from core.app.entities.app_invoke_entities import InvokeFrom
from extensions.ext_redis import redis_client
from graphon.graph_engine.manager import GraphEngineManager
from models.model import AppMode


class AppTaskService:
    """Service for managing application task operations."""

    @staticmethod
    def stop_task(
        task_id: str,
        invoke_from: InvokeFrom,
        user_id: str,
        app_mode: AppMode,
    ) -> None:
        """Stop a running task, iff `user_id` owns `task_id`.

        This method handles stopping tasks using both mechanisms:
        1. Legacy Redis flag mechanism (for backward compatibility)
        2. New GraphEngine command channel (for workflow-based apps)

        Ownership of `task_id` by `user_id`/`invoke_from` is checked once, via
        `AppQueueManager.set_stop_flag`, before either mechanism runs. If the
        task is unknown or belongs to someone else, this is a silent no-op for
        both mechanisms -- callers must not bypass this by invoking
        `AppQueueManager.set_stop_flag_no_user_check` or `GraphEngineManager`
        directly from a user-authenticated route.

        Args:
            task_id: The task ID to stop
            invoke_from: The source of the invoke (e.g., DEBUGGER, WEB_APP, SERVICE_API)
            user_id: The user ID requesting the stop
            app_mode: The application mode (CHAT, AGENT_CHAT, ADVANCED_CHAT, WORKFLOW, etc.)

        Returns:
            None
        """
        # Legacy mechanism: Set stop flag in Redis, but only if `user_id` owns
        # `task_id` (checked against the `generate_task_belong:<task_id>` redis
        # key). A mismatched or unknown task_id is a silent no-op -- this is
        # the sole ownership gate for this task, so it must run before any
        # other stop mechanism is allowed to fire.
        owns_task = AppQueueManager.set_stop_flag(task_id, invoke_from, user_id)
        if not owns_task:
            return

        # New mechanism: Send stop command via GraphEngine for workflow-based apps
        # This ensures proper workflow status recording in the persistence layer.
        # Only reached once ownership is confirmed above, so this can't be used
        # to stop another tenant's/user's task.
        if app_mode in (AppMode.ADVANCED_CHAT, AppMode.WORKFLOW):
            GraphEngineManager(redis_client).send_stop_command(task_id)

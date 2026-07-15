from collections.abc import Callable
from functools import wraps

from sqlalchemy import select
from sqlalchemy.orm import Session
from werkzeug.exceptions import Forbidden

from controllers.console.datasets.error import PipelineNotFoundError
from extensions.ext_database import db
from libs.login import current_account_with_tenant
from models.dataset import Pipeline
from services.dataset_service import DatasetService
from services.errors.account import NoPermissionError


def get_rag_pipeline[**P, R](view_func: Callable[P, R]) -> Callable[P, R]:
    @wraps(view_func)
    def decorated_view(*args: P.args, **kwargs: P.kwargs) -> R:
        if not kwargs.get("pipeline_id"):
            raise ValueError("missing pipeline_id in path parameters")

        current_user, current_tenant_id = current_account_with_tenant()

        pipeline_id = kwargs.get("pipeline_id")
        pipeline_id = str(pipeline_id)

        del kwargs["pipeline_id"]

        stmt = select(Pipeline).where(Pipeline.id == pipeline_id, Pipeline.tenant_id == current_tenant_id).limit(1)
        # Migrated handlers pass the request Session as args[1]; legacy handlers still use db.session.
        session = args[1] if len(args) > 1 and isinstance(args[1], Session) else db.session
        pipeline = session.scalar(stmt)

        if not pipeline:
            raise PipelineNotFoundError()

        # The tenant check above only proves the caller belongs to the same workspace; it does not
        # enforce the underlying dataset's own privacy setting (e.g. only_me / partial_team), which
        # every dataset-scoped console route enforces via DatasetService.check_dataset_permission.
        # Mirror that check here so pipeline-scoped routes (export/import/workflow/etc.) inherit it.
        dataset = pipeline.retrieve_dataset(session)
        if dataset is not None:
            try:
                DatasetService.check_dataset_permission(dataset, current_user, session)
            except NoPermissionError as e:
                raise Forbidden(str(e))

        kwargs["pipeline"] = pipeline

        return view_func(*args, **kwargs)

    return decorated_view

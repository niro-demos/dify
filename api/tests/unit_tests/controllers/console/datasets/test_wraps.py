from unittest.mock import Mock

import pytest
from pytest_mock import MockerFixture
from werkzeug.exceptions import Forbidden

import services
from controllers.console.datasets.error import PipelineNotFoundError
from controllers.console.datasets.wraps import get_rag_pipeline
from models.dataset import Dataset, Pipeline


def _make_pipeline(*, dataset: Dataset | None = None) -> Mock:
    """A Pipeline mock whose retrieve_dataset() returns ``dataset`` (None by default,
    i.e. no dataset is associated with the pipeline)."""
    pipeline = Mock(spec=Pipeline)
    pipeline.id = "pipeline-1"
    pipeline.tenant_id = "tenant-1"
    pipeline.retrieve_dataset = Mock(return_value=dataset)
    return pipeline


class TestGetRagPipeline:
    def test_missing_pipeline_id(self):
        @get_rag_pipeline
        def dummy_view(**kwargs):
            return "ok"

        with pytest.raises(ValueError, match="missing pipeline_id"):
            dummy_view()

    def test_pipeline_not_found(self, mocker: MockerFixture):
        @get_rag_pipeline
        def dummy_view(**kwargs):
            return "ok"

        mocker.patch(
            "controllers.console.datasets.wraps.current_account_with_tenant",
            return_value=(Mock(), "tenant-1"),
        )

        mocker.patch(
            "controllers.console.datasets.wraps.db.session.scalar",
            return_value=None,
        )

        with pytest.raises(PipelineNotFoundError):
            dummy_view(pipeline_id="pipeline-1")

    def test_pipeline_found_and_injected(self, mocker: MockerFixture):
        pipeline = _make_pipeline()

        @get_rag_pipeline
        def dummy_view(**kwargs):
            return kwargs["pipeline"]

        mocker.patch(
            "controllers.console.datasets.wraps.current_account_with_tenant",
            return_value=(Mock(), "tenant-1"),
        )

        mocker.patch(
            "controllers.console.datasets.wraps.db.session.scalar",
            return_value=pipeline,
        )

        result = dummy_view(pipeline_id="pipeline-1")

        assert result is pipeline

    def test_pipeline_id_removed_from_kwargs(self, mocker: MockerFixture):
        pipeline = _make_pipeline()

        @get_rag_pipeline
        def dummy_view(**kwargs):
            assert "pipeline_id" not in kwargs
            return "ok"

        mocker.patch(
            "controllers.console.datasets.wraps.current_account_with_tenant",
            return_value=(Mock(), "tenant-1"),
        )

        mocker.patch(
            "controllers.console.datasets.wraps.db.session.scalar",
            return_value=pipeline,
        )

        result = dummy_view(pipeline_id="pipeline-1")

        assert result == "ok"

    def test_pipeline_id_cast_to_string(self, mocker: MockerFixture):
        pipeline = _make_pipeline()

        @get_rag_pipeline
        def dummy_view(**kwargs):
            return kwargs["pipeline"]

        mocker.patch(
            "controllers.console.datasets.wraps.current_account_with_tenant",
            return_value=(Mock(), "tenant-1"),
        )

        mock_scalar = mocker.patch(
            "controllers.console.datasets.wraps.db.session.scalar",
            return_value=pipeline,
        )

        result = dummy_view(pipeline_id=123)

        assert result is pipeline
        # Verify the pipeline_id was cast to string in the where clause
        stmt = mock_scalar.call_args[0][0]
        where_clauses = stmt.whereclause.clauses
        assert where_clauses[0].right.value == "123"


class TestGetRagPipelineDatasetPermission:
    """Regression tests for TC-D4B3171D: routes decorated with get_rag_pipeline must
    inherit the underlying dataset's only_me/partial_team/maintainer privacy boundary,
    not just the caller's own-tenant membership."""

    def test_no_associated_dataset_skips_permission_check(self, mocker: MockerFixture):
        """A pipeline with no linked dataset (retrieve_dataset() -> None) is not gated
        by DatasetService.check_dataset_permission at all."""
        pipeline = _make_pipeline(dataset=None)

        @get_rag_pipeline
        def dummy_view(**kwargs):
            return kwargs["pipeline"]

        mocker.patch(
            "controllers.console.datasets.wraps.current_account_with_tenant",
            return_value=(Mock(), "tenant-1"),
        )
        mocker.patch("controllers.console.datasets.wraps.db.session.scalar", return_value=pipeline)
        check_permission = mocker.patch.object(services.dataset_service.DatasetService, "check_dataset_permission")

        result = dummy_view(pipeline_id="pipeline-1")

        assert result is pipeline
        check_permission.assert_not_called()

    def test_only_me_dataset_denies_non_maintainer(self, mocker: MockerFixture):
        """Same-tenant caller who is NOT the maintainer of an only_me dataset must be
        rejected from pipeline-scoped routes (export/import/workflow/etc), matching the
        dataset-detail endpoint's own behavior for the identical resource."""
        dataset = Mock(spec=Dataset)
        dataset.tenant_id = "tenant-1"
        dataset.permission = "only_me"
        dataset.maintainer = "owner-account-id"
        dataset.id = "dataset-1"

        pipeline = _make_pipeline(dataset=dataset)

        current_user = Mock()
        current_user.id = "editor-account-id"
        current_user.current_tenant_id = "tenant-1"
        current_user.current_role = "editor"

        @get_rag_pipeline
        def dummy_view(**kwargs):
            return kwargs["pipeline"]

        mocker.patch(
            "controllers.console.datasets.wraps.current_account_with_tenant",
            return_value=(current_user, "tenant-1"),
        )
        mocker.patch("controllers.console.datasets.wraps.db.session.scalar", return_value=pipeline)

        with pytest.raises(Forbidden):
            dummy_view(pipeline_id="pipeline-1")

    def test_only_me_dataset_allows_maintainer(self, mocker: MockerFixture):
        """The dataset's own maintainer keeps access through pipeline-scoped routes."""
        dataset = Mock(spec=Dataset)
        dataset.tenant_id = "tenant-1"
        dataset.permission = "only_me"
        dataset.maintainer = "owner-account-id"
        dataset.id = "dataset-1"

        pipeline = _make_pipeline(dataset=dataset)

        current_user = Mock()
        current_user.id = "owner-account-id"
        current_user.current_tenant_id = "tenant-1"
        current_user.current_role = "owner"

        @get_rag_pipeline
        def dummy_view(**kwargs):
            return kwargs["pipeline"]

        mocker.patch(
            "controllers.console.datasets.wraps.current_account_with_tenant",
            return_value=(current_user, "tenant-1"),
        )
        mocker.patch("controllers.console.datasets.wraps.db.session.scalar", return_value=pipeline)

        result = dummy_view(pipeline_id="pipeline-1")

        assert result is pipeline

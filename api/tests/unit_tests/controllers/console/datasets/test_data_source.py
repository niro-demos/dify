from __future__ import annotations

import inspect
from collections.abc import Callable
from datetime import UTC, datetime
from typing import cast
from unittest.mock import MagicMock, PropertyMock, patch

import pytest
from flask import Flask
from werkzeug.exceptions import Forbidden, NotFound

import services
from controllers.console.datasets import data_source as module
from controllers.console.datasets.data_source import (
    DataSourceApi,
    DataSourceNotionDatasetSyncApi,
    DataSourceNotionDocumentSyncApi,
    DataSourceNotionListApi,
)
from models import Account, DataSourceOauthBinding

ControllerMethod = Callable[..., tuple[dict[str, object], int]]


def unwrap(func: object) -> ControllerMethod:
    return cast(ControllerMethod, inspect.unwrap(cast(Callable[..., object], func)))


@pytest.fixture
def flask_app() -> Flask:
    app = Flask(__name__)
    app.config["TESTING"] = True
    return app


@pytest.fixture
def current_user() -> Account:
    account = Account(name="Test User", email="user-1@example.com")
    account.id = "user-1"
    return account


def test_get_data_source_integrates_serializes_orm_binding(flask_app: Flask) -> None:
    binding = DataSourceOauthBinding(
        tenant_id="tenant-1",
        access_token="token",
        provider="notion",
        source_info={
            "workspace_name": "Workspace",
            "workspace_id": "workspace-1",
            "workspace_icon": None,
            "total": 1,
            "pages": [
                {
                    "page_id": "page-1",
                    "page_name": "Page",
                    "page_icon": {"type": "emoji", "emoji": "P", "url": None},
                    "parent_id": "parent-1",
                    "type": "page",
                }
            ],
        },
    )
    binding.id = "binding-1"
    binding.created_at = datetime(2026, 5, 25, 1, 2, 3, tzinfo=UTC)
    binding.disabled = False

    with (
        flask_app.test_request_context("/"),
        patch.object(module.db.session, "scalars", return_value=MagicMock(all=lambda: [binding])),
    ):
        response, status = unwrap(DataSourceApi().get)(DataSourceApi(), "tenant-1")

    assert status == 200
    assert response == {
        "data": [
            {
                "id": "binding-1",
                "provider": "notion",
                "created_at": 1779670923,
                "is_bound": True,
                "disabled": False,
                "source_info": {
                    "workspace_name": "Workspace",
                    "workspace_id": "workspace-1",
                    "workspace_icon": None,
                    "pages": [
                        {
                            "page_name": "Page",
                            "page_id": "page-1",
                            "page_icon": {"type": "emoji", "url": None, "emoji": "P"},
                            "parent_id": "parent-1",
                            "type": "page",
                        }
                    ],
                    "total": 1,
                },
                "link": "http://localhost/console/api/oauth/data-source/notion",
            }
        ]
    }


def test_get_data_source_integrates_preserves_empty_list_when_no_binding(flask_app: Flask) -> None:
    with (
        flask_app.test_request_context("/"),
        patch.object(module.db.session, "scalars", return_value=MagicMock(all=lambda: [])),
    ):
        response, status = unwrap(DataSourceApi().get)(DataSourceApi(), "tenant-1")

    assert status == 200
    assert response == {"data": []}


def test_notion_pre_import_pages_serializes_frontend_list_shape(flask_app: Flask, current_user: Account) -> None:
    page = MagicMock(
        page_id="page-1",
        page_name="Page",
        type="page",
        parent_id="parent-1",
        page_icon={"type": "emoji", "emoji": "P", "url": None},
    )
    online_document_message = MagicMock(
        result=[
            MagicMock(
                workspace_id="workspace-1",
                workspace_name="Workspace",
                workspace_icon=None,
                pages=[page],
            )
        ]
    )
    runtime = MagicMock(
        get_online_document_pages=MagicMock(return_value=iter([online_document_message])),
        datasource_provider_type=MagicMock(return_value="online_document"),
    )

    with (
        flask_app.test_request_context("/?credential_id=credential-1"),
        patch.object(
            module.DatasourceProviderService,
            "get_datasource_credentials",
            return_value={"token": "token"},
        ),
        patch.object(type(module.db), "engine", new_callable=PropertyMock, return_value=MagicMock()),
        patch.object(module, "sessionmaker"),
        patch("core.datasource.datasource_manager.DatasourceManager.get_datasource_runtime", return_value=runtime),
    ):
        response, status = unwrap(DataSourceNotionListApi().get)(DataSourceNotionListApi(), "tenant-1", current_user)

    assert status == 200
    assert response == {
        "notion_info": [
            {
                "workspace_name": "Workspace",
                "workspace_id": "workspace-1",
                "workspace_icon": None,
                "pages": [
                    {
                        "page_name": "Page",
                        "page_id": "page-1",
                        "page_icon": {"type": "emoji", "url": None, "emoji": "P"},
                        "parent_id": "parent-1",
                        "type": "page",
                        "is_bound": False,
                    }
                ],
            }
        ]
    }
    runtime.get_online_document_pages.assert_called_once()
    assert runtime.get_online_document_pages.call_args.kwargs["datasource_parameters"] == {}


def _make_dataset(tenant_id: str = "tenant-1") -> MagicMock:
    dataset = MagicMock()
    dataset.id = "dataset-1"
    dataset.tenant_id = tenant_id
    return dataset


class TestDataSourceNotionDatasetSyncApi:
    def test_sync_success(self, flask_app: Flask, current_user: Account) -> None:
        dataset = _make_dataset()
        documents = [MagicMock(id="doc-1"), MagicMock(id="doc-2")]

        with (
            flask_app.test_request_context("/"),
            patch.object(module.DatasetService, "get_dataset", return_value=dataset),
            patch.object(module.DatasetService, "check_dataset_permission", return_value=None),
            patch.object(module.DocumentService, "get_document_by_dataset_id", return_value=documents),
            patch.object(module.document_indexing_sync_task, "delay") as delay_mock,
        ):
            response, status = unwrap(DataSourceNotionDatasetSyncApi().get)(
                DataSourceNotionDatasetSyncApi(), current_user, "dataset-1"
            )

        assert status == 200
        assert response == {"result": "success"}
        assert delay_mock.call_count == 2

    def test_sync_dataset_not_found(self, flask_app: Flask, current_user: Account) -> None:
        with (
            flask_app.test_request_context("/"),
            patch.object(module.DatasetService, "get_dataset", return_value=None),
        ):
            with pytest.raises(NotFound):
                unwrap(DataSourceNotionDatasetSyncApi().get)(
                    DataSourceNotionDatasetSyncApi(), current_user, "dataset-1"
                )

    def test_sync_cross_tenant_denied(self, flask_app: Flask, current_user: Account) -> None:
        """Regression test for TC-42C6CE80: a user from another tenant must not be able to
        trigger a re-indexing/sync job on another workspace's dataset by guessing its id."""
        other_tenant_dataset = _make_dataset(tenant_id="other-tenant")

        with (
            flask_app.test_request_context("/"),
            patch.object(module.DatasetService, "get_dataset", return_value=other_tenant_dataset),
            patch.object(
                module.DatasetService,
                "check_dataset_permission",
                side_effect=services.errors.account.NoPermissionError("no access"),
            ),
            patch.object(module.document_indexing_sync_task, "delay") as delay_mock,
        ):
            with pytest.raises(Forbidden):
                unwrap(DataSourceNotionDatasetSyncApi().get)(
                    DataSourceNotionDatasetSyncApi(), current_user, "dataset-1"
                )

        delay_mock.assert_not_called()


class TestDataSourceNotionDocumentSyncApi:
    def test_sync_success(self, flask_app: Flask, current_user: Account) -> None:
        dataset = _make_dataset()
        document = MagicMock(id="doc-1")

        with (
            flask_app.test_request_context("/"),
            patch.object(module.DatasetService, "get_dataset", return_value=dataset),
            patch.object(module.DatasetService, "check_dataset_permission", return_value=None),
            patch.object(module.DocumentService, "get_document", return_value=document),
            patch.object(module.document_indexing_sync_task, "delay") as delay_mock,
        ):
            response, status = unwrap(DataSourceNotionDocumentSyncApi().get)(
                DataSourceNotionDocumentSyncApi(), current_user, "dataset-1", "doc-1"
            )

        assert status == 200
        assert response == {"result": "success"}
        delay_mock.assert_called_once_with("dataset-1", "doc-1")

    def test_sync_dataset_not_found(self, flask_app: Flask, current_user: Account) -> None:
        with (
            flask_app.test_request_context("/"),
            patch.object(module.DatasetService, "get_dataset", return_value=None),
        ):
            with pytest.raises(NotFound):
                unwrap(DataSourceNotionDocumentSyncApi().get)(
                    DataSourceNotionDocumentSyncApi(), current_user, "dataset-1", "doc-1"
                )

    def test_sync_cross_tenant_denied(self, flask_app: Flask, current_user: Account) -> None:
        """Regression test for TC-42C6CE80: a user from another tenant must not be able to
        trigger a re-indexing/sync job on another workspace's document by guessing its id."""
        other_tenant_dataset = _make_dataset(tenant_id="other-tenant")

        with (
            flask_app.test_request_context("/"),
            patch.object(module.DatasetService, "get_dataset", return_value=other_tenant_dataset),
            patch.object(
                module.DatasetService,
                "check_dataset_permission",
                side_effect=services.errors.account.NoPermissionError("no access"),
            ),
            patch.object(module.document_indexing_sync_task, "delay") as delay_mock,
        ):
            with pytest.raises(Forbidden):
                unwrap(DataSourceNotionDocumentSyncApi().get)(
                    DataSourceNotionDocumentSyncApi(), current_user, "dataset-1", "doc-1"
                )

        delay_mock.assert_not_called()

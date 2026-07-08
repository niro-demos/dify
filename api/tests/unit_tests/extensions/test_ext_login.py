import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from flask import Response
from werkzeug.exceptions import Unauthorized

from extensions.ext_login import load_user_from_request, unauthorized_handler
from services.account_service import AccountService


class _RedisFake:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def setex(self, key: str, _expiry: object, value: str) -> None:
        self.values[key] = value.encode("utf-8")

    def get(self, key: str) -> bytes | None:
        return self.values.get(key)

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


def test_unauthorized_handler_returns_json_response() -> None:
    response = unauthorized_handler()

    assert isinstance(response, Response)
    assert response.status_code == 401
    assert response.content_type == "application/json"
    assert json.loads(response.get_data(as_text=True)) == {
        "code": "unauthorized",
        "message": "Unauthorized.",
    }


def test_console_access_token_is_rejected_after_logout(app, monkeypatch: pytest.MonkeyPatch) -> None:
    redis_fake = _RedisFake()
    monkeypatch.setattr("libs.passport.dify_config.SECRET_KEY", "test-secret-long-enough-for-hs256")
    monkeypatch.setattr("services.account_service.redis_client", redis_fake)

    account = SimpleNamespace(id="account-1", status="active")
    monkeypatch.setattr(AccountService, "load_logged_in_account", MagicMock(return_value=account))
    token_pair = AccountService.login(account, session=MagicMock())

    @app.route("/console/api/info", methods=["POST"], endpoint="console.info")
    def _console_info() -> str:
        return "ok"

    with app.test_request_context(
        "/console/api/info",
        method="POST",
        headers={"Authorization": f"Bearer {token_pair.access_token}"},
    ):
        assert load_user_from_request(MagicMock()) is account

    AccountService.logout(account=account)

    with (
        app.test_request_context(
            "/console/api/info",
            method="POST",
            headers={"Authorization": f"Bearer {token_pair.access_token}"},
        ),
        pytest.raises(Unauthorized, match="Invalid Authorization token."),
    ):
        load_user_from_request(MagicMock())

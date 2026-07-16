import json
from unittest.mock import MagicMock

import pytest
from flask import Blueprint, Flask, Response
from werkzeug.exceptions import Unauthorized

from extensions import ext_login
from extensions.ext_login import _load_user_from_request, unauthorized_handler


def test_unauthorized_handler_returns_json_response() -> None:
    response = unauthorized_handler()

    assert isinstance(response, Response)
    assert response.status_code == 401
    assert response.content_type == "application/json"
    assert json.loads(response.get_data(as_text=True)) == {
        "code": "unauthorized",
        "message": "Unauthorized.",
    }


@pytest.fixture
def console_request_context():
    """A Flask request context whose `request.blueprint` resolves to "console",
    matching the branch of `_load_user_from_request` that authenticates the
    web console via a bearer/cookie access token."""
    app = Flask(__name__)
    app.config["TESTING"] = True
    bp = Blueprint("console", __name__)

    @bp.route("/probe")
    def probe():
        return "ok"

    app.register_blueprint(bp)
    with app.test_request_context("/probe"):
        yield


class TestLoadUserFromRequestRejectsStaleAccessTokens:
    """TC-63F7F852 regression: an access token issued before the account last
    revoked its sessions (e.g. a password change) must stop being honoured on
    the very next request, not merely once its own TTL elapses.
    """

    def test_stale_access_token_is_rejected(self, console_request_context, monkeypatch):
        monkeypatch.setattr(ext_login, "extract_access_token", lambda _request: "stale-token")
        monkeypatch.setattr(
            ext_login.PassportService,
            "verify",
            lambda self, token: {"user_id": "acc-1", "iat": 1_000},
        )
        monkeypatch.setattr(ext_login.AccountService, "is_access_token_stale", lambda account_id, iat: True)
        load_account = MagicMock()
        monkeypatch.setattr(ext_login.AccountService, "load_logged_in_account", load_account)

        with pytest.raises(Unauthorized):
            _load_user_from_request(MagicMock(), session=MagicMock())

        load_account.assert_not_called()

    def test_fresh_access_token_is_still_accepted(self, console_request_context, monkeypatch):
        """Positive control: a token that isn't stale must still authenticate,
        proving the rejection above is specific to staleness and not a broken
        request-loading path."""
        monkeypatch.setattr(ext_login, "extract_access_token", lambda _request: "fresh-token")
        monkeypatch.setattr(
            ext_login.PassportService,
            "verify",
            lambda self, token: {"user_id": "acc-1", "iat": 2_000},
        )
        monkeypatch.setattr(ext_login.AccountService, "is_access_token_stale", lambda account_id, iat: False)
        sentinel_account = MagicMock()
        monkeypatch.setattr(ext_login.AccountService, "load_logged_in_account", lambda **kwargs: sentinel_account)

        result = _load_user_from_request(MagicMock(), session=MagicMock())

        assert result is sentinel_account

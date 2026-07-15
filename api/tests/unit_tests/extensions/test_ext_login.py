import json
from types import SimpleNamespace

import pytest
from flask import Blueprint, Flask, Response
from werkzeug.exceptions import Unauthorized

import extensions.ext_login as ext_login_module
from configs import dify_config
from extensions.ext_login import load_user_from_request, unauthorized_handler
from libs.passport import PassportService
from services.account_service import AccountService


def test_unauthorized_handler_returns_json_response() -> None:
    response = unauthorized_handler()

    assert isinstance(response, Response)
    assert response.status_code == 401
    assert response.content_type == "application/json"
    assert json.loads(response.get_data(as_text=True)) == {
        "code": "unauthorized",
        "message": "Unauthorized.",
    }


class _FakeRedis:
    """Minimal in-memory stand-in for redis_client, keyed exactly like the real
    client (setex/exists/get/delete), so the denylist actually holds state across
    calls instead of the module-level MagicMock (which has no real storage)."""

    def __init__(self) -> None:
        self._store: dict[str, object] = {}

    def setex(self, name: str, time: int, value: object) -> None:
        self._store[str(name)] = value

    def exists(self, name: str) -> int:
        return 1 if str(name) in self._store else 0

    def get(self, name: str) -> object | None:
        return self._store.get(str(name))

    def delete(self, *names: str) -> None:
        for name in names:
            self._store.pop(str(name), None)


@pytest.fixture
def console_app() -> Flask:
    """A Flask app with a route registered under a 'console' blueprint, so
    `request.blueprint == "console"` resolves the same way it does in the real app."""
    app = Flask(__name__)
    app.config["TESTING"] = True
    bp = Blueprint("console", __name__)

    @bp.route("/console/api/account/profile")
    def profile():
        return "ok"

    app.register_blueprint(bp)
    return app


@pytest.fixture
def fake_access_token_redis(monkeypatch: pytest.MonkeyPatch) -> _FakeRedis:
    """Route AccountService's redis_client at the *stateful* fake for this test only,
    so revoke_access_token()/is_access_token_revoked() interact with real storage."""
    fake = _FakeRedis()
    monkeypatch.setattr("services.account_service.redis_client", fake)
    return fake


@pytest.fixture
def stub_account_loading(monkeypatch: pytest.MonkeyPatch):
    """Bypass real DB access: `load_user_from_request` calls
    AccountService.load_logged_in_account(...) and db.session(), neither of which
    this test needs, since it is only exercising the auth *gate* in front of them."""
    account = SimpleNamespace(id="account-1")
    monkeypatch.setattr(
        AccountService,
        "load_logged_in_account",
        staticmethod(lambda *, account_id, session: account),
    )
    monkeypatch.setattr(ext_login_module, "db", SimpleNamespace(session=lambda: None))
    return account


class TestConsoleRequestLoaderRevokesAccessTokenOnLogout:
    """TC-2322148C: a captured access_token must stop working the moment the
    account it belongs to logs out -- not just when the JWT's own exp elapses."""

    def test_stale_access_token_is_rejected_after_logout(
        self,
        console_app: Flask,
        fake_access_token_redis: _FakeRedis,
        stub_account_loading: SimpleNamespace,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(dify_config, "SECRET_KEY", "test-secret-key-for-tc-2322148c-32-bytes-min")
        account = stub_account_loading
        access_token = AccountService.get_account_jwt_token(account)  # type: ignore[arg-type]
        jti = PassportService().verify(access_token)["jti"]

        # Control: the token is accepted before logout (proves this isn't a broken
        # setup / an always-401 environment).
        with console_app.test_request_context(
            "/console/api/account/profile",
            headers={"Cookie": f"access_token={access_token}"},
        ):
            assert load_user_from_request(None) is account  # type: ignore[arg-type]

        AccountService.logout(account=account, access_token_jti=jti)  # type: ignore[arg-type]

        # The captured, pre-logout access_token is replayed unchanged.
        with console_app.test_request_context(
            "/console/api/account/profile",
            headers={"Cookie": f"access_token={access_token}"},
        ):
            with pytest.raises(Unauthorized):
                load_user_from_request(None)  # type: ignore[arg-type]

"""CORS posture for /console/api/* — credentialed origins must be an
explicit allowlist, never the wildcard ``*``.

When ``supports_credentials=True``, flask-cors cannot emit
``Access-Control-Allow-Origin: *`` (forbidden by the CORS spec with
credentials). Instead it reflects the request ``Origin`` verbatim, which
allows any website to make credentialed requests — enabling a forced-login
attack where a victim is silently logged into an attacker's account.

The ``_sanitize_credentialed_origins`` helper strips ``*`` (and empty
strings) so only explicit, trusted origins are admitted. With an empty
result, flask-cors admits no cross-origin request (same-origin only).

Tests use a fresh Blueprint + Flask-CORS per case because the production
blueprint is a module-level singleton and can't be reconfigured once
registered (same approach as ``test_ext_blueprints_openapi.py``).
"""

from flask import Blueprint, Flask
from flask_cors import CORS

from extensions.ext_blueprints import (
    AUTHENTICATED_HEADERS,
    EXPOSED_HEADERS,
    _sanitize_credentialed_origins,
)

_EVIL_ORIGIN = "https://evil.com"
_ALLOWED_ORIGIN = "https://app.example.com"


def _make_console_app(origins: list[str]) -> Flask:
    """Build a Flask app mirroring the console blueprint CORS config,
    applying the same origin sanitization as production.
    """
    sanitized = _sanitize_credentialed_origins(origins)
    bp = Blueprint("console_cors_test", __name__, url_prefix="/console/api")

    @bp.route("/_health")
    def _health():
        return {"ok": True}

    CORS(
        bp,
        resources={r"/*": {"origins": sanitized}},
        supports_credentials=True,
        allow_headers=list(AUTHENTICATED_HEADERS),
        methods=["GET", "PUT", "POST", "DELETE", "OPTIONS", "PATCH"],
        expose_headers=list(EXPOSED_HEADERS),
    )

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(bp)
    return app


# ---------------------------------------------------------------------------
# Unit tests for the sanitizer helper
# ---------------------------------------------------------------------------


class TestSanitizeCredentialedOrigins:
    def test_wildcard_removed(self):
        assert _sanitize_credentialed_origins(["*"]) == []

    def test_wildcard_removed_from_mixed_list(self):
        result = _sanitize_credentialed_origins([_ALLOWED_ORIGIN, "*"])
        assert result == [_ALLOWED_ORIGIN]

    def test_empty_list_stays_empty(self):
        assert _sanitize_credentialed_origins([]) == []

    def test_explicit_origins_preserved(self):
        origins = [_ALLOWED_ORIGIN, "https://console.example.com"]
        assert _sanitize_credentialed_origins(origins) == origins

    def test_empty_string_removed(self):
        result = _sanitize_credentialed_origins(["", _ALLOWED_ORIGIN])
        assert result == [_ALLOWED_ORIGIN]


# ---------------------------------------------------------------------------
# Integration tests — full CORS behavior through a Flask test client
# ---------------------------------------------------------------------------


class TestConsoleCorsBehavior:
    """The core invariant: a credentialed request from an arbitrary origin
    must not receive ``Access-Control-Allow-Origin`` reflection."""

    def test_wildcard_config_does_not_reflect_arbitrary_origin(self):
        """With ``origins=['*']`` (the dangerous default), sanitization
        strips the wildcard so flask-cors does NOT reflect the evil origin."""
        app = _make_console_app(["*"])
        client = app.test_client()
        response = client.get("/console/api/_health", headers={"Origin": _EVIL_ORIGIN})
        assert "Access-Control-Allow-Origin" not in response.headers

    def test_wildcard_config_preflight_does_not_reflect_arbitrary_origin(self):
        app = _make_console_app(["*"])
        client = app.test_client()
        response = client.options(
            "/console/api/_health",
            headers={
                "Origin": _EVIL_ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        assert "Access-Control-Allow-Origin" not in response.headers

    def test_null_origin_not_reflected_with_wildcard_config(self):
        app = _make_console_app(["*"])
        client = app.test_client()
        response = client.get("/console/api/_health", headers={"Origin": "null"})
        assert "Access-Control-Allow-Origin" not in response.headers

    def test_allowlisted_origin_is_reflected(self):
        """A legitimate, explicitly-allowed origin still gets CORS headers."""
        app = _make_console_app([_ALLOWED_ORIGIN])
        client = app.test_client()
        response = client.get("/console/api/_health", headers={"Origin": _ALLOWED_ORIGIN})
        assert response.headers.get("Access-Control-Allow-Origin") == _ALLOWED_ORIGIN
        assert response.headers.get("Access-Control-Allow-Credentials") == "true"

    def test_disallowed_origin_not_reflected_with_explicit_allowlist(self):
        app = _make_console_app([_ALLOWED_ORIGIN])
        client = app.test_client()
        response = client.get("/console/api/_health", headers={"Origin": _EVIL_ORIGIN})
        assert "Access-Control-Allow-Origin" not in response.headers

    def test_same_origin_request_without_origin_header(self):
        """Same-origin requests (no Origin header) still work normally."""
        app = _make_console_app(["*"])
        client = app.test_client()
        response = client.get("/console/api/_health")
        assert response.status_code == 200
        assert response.get_json() == {"ok": True}

"""Renders niro/credentials.yaml and niro/fixtures.yaml from the JSON blob
seed_accounts.py prints. Run on the host (not inside a container) by
seed.sh, after it has extracted that JSON from the seeded container's
stdout.

Kept separate from seed_accounts.py because the descriptive/auth-mechanics
text below (console cookie+CSRF flow, API-token placement, pairing intent)
belongs with the generator, not scattered as inline strings inside the
in-container bootstrap script.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Kept short on purpose -- credentials.yaml descriptions are capped at 1000
# runes. The full verified mechanics (base64 password quirk, cookie names,
# CSRF header requirement, live-verified proof) live in the
# "console_auth_mechanics" fixture in fixtures.yaml, which has no cap.
CONSOLE_LOGIN_NOTE = (
    "Login: POST /console/api/login {email, password}; password must be "
    "base64-encoded first (plaintext 401s). Sets access_token/refresh_token/"
    "csrf_token cookies; send header X-CSRF-Token = csrf_token cookie value "
    "on every authenticated request (GET included) or 401. Full recipe: "
    "fixture console_auth_mechanics."
)


def account_description(role: str, tenant_label: str, owns: str, pairing: str, extra: str = "") -> str:
    parts = [f"Role: {role} in {tenant_label}.", f"Owns {owns}.", CONSOLE_LOGIN_NOTE, pairing]
    if extra:
        parts.append(extra)
    text = " ".join(p.strip() for p in parts if p.strip())
    if len(text) > 1000:
        text = text[:997] + "..."
    return text


def main() -> None:
    data = json.loads(sys.stdin.read())
    accounts = data["accounts"]
    org_a = data["org_a"]
    org_b = data["org_b"]
    tenant_a = data["tenant_a"]
    tenant_b = data["tenant_b"]

    creds = []

    def add_user(cred_id: str, key: str, role: str, tenant_label: str, owns: str, pairing: str, extra: str = ""):
        a = accounts[key]
        creds.append(
            {
                "credential_id": cred_id,
                "description": account_description(role, tenant_label, owns, pairing, extra),
                "type": "username_password",
                "identifier": a["email"],
                "secret": a["password"],
            }
        )

    add_user(
        "OWNER_A",
        "owner_a",
        "owner",
        "Org A (Niro Org A)",
        f"Org A's chat app ({org_a['app_id']}) and dataset ({org_a['dataset_id']})",
        "Pair with OWNER_B for cross-tenant/horizontal isolation tests: authenticate as Owner A, "
        "attempt to read/modify Org B's app or dataset by id, expect 403/404. Full owner privileges "
        "within Org A: billing, member management, all app/dataset CRUD.",
    )
    add_user(
        "ADMIN_A",
        "admin_a",
        "admin",
        "Org A",
        "no app/dataset of its own -- created to test admin-scoped surfaces (member management, "
        "workspace settings) against Org A's resources",
        "Pair with MEMBER_A1/MEMBER_A2/EDITOR_A/OPERATOR_A for vertical-escalation tests within Org A: "
        "admin should reach member-management and workspace-settings endpoints those lower roles must "
        "be denied.",
    )
    add_user(
        "EDITOR_A",
        "editor_a",
        "editor",
        "Org A",
        "no app/dataset of its own",
        "Can create/edit apps and datasets but not manage members/billing (owner/admin-only). Use to "
        "verify editor is denied at admin-only endpoints (member invite/remove, workspace transfer) "
        "while still allowed normal app/dataset CRUD.",
    )
    add_user(
        "MEMBER_A1",
        "member_a1",
        "normal",
        "Org A",
        f"Org A's chat app ({org_a['app_id']}) and dataset ({org_a['dataset_id']}) are OWNED BY OWNER_A, "
        "not by this member -- Member A1 only has whatever tenant-wide normal-role access Org A grants",
        "Pair with MEMBER_A2: same role, different identity, for horizontal-escalation checks on any "
        "per-member resource (e.g. personal API tokens, notification settings) that should not be "
        "readable across members even within the same tenant.",
    )
    add_user(
        "MEMBER_A2",
        "member_a2",
        "normal",
        "Org A",
        "same tenant-wide access as MEMBER_A1, no personally-owned app/dataset",
        "Pair with MEMBER_A1 (see above).",
    )
    add_user(
        "OPERATOR_A",
        "operator_a",
        "dataset_operator",
        "Org A",
        "no app/dataset of its own",
        "Scoped to dataset/knowledge-base operations only -- should be denied app-management and "
        "member-management endpoints. Use to verify the dataset_operator role cannot escalate to "
        "editor/admin-only app surfaces.",
    )
    add_user(
        "OWNER_B",
        "owner_b",
        "owner",
        "Org B (Niro Org B)",
        f"Org B's chat app ({org_b['app_id']}) and dataset ({org_b['dataset_id']})",
        "Pair with OWNER_A for cross-tenant isolation tests (see OWNER_A description). Org B is a "
        "wholly separate tenant/workspace from Org A -- no shared membership.",
    )
    add_user(
        "MEMBER_B1",
        "member_b1",
        "normal",
        "Org B",
        "no personally-owned app/dataset",
        "Same-tenant normal member in Org B, for within-Org-B vertical-escalation checks against "
        "OWNER_B, and cross-tenant checks against Org A's normal members.",
    )

    creds.append(
        {
            "credential_id": "ORG_A_APP_API_KEY",
            "description": (
                f"Public Service API key for Org A's seeded chat app (app_id {org_a['app_id']}, tenant "
                f"{tenant_a['id']}). Send as 'Authorization: Bearer <token>' against /v1/* app endpoints "
                "(chat-messages, completion-messages, etc). No cookies/CSRF involved -- this is the "
                "external developer-facing API, separate from the console session auth above. Pair with "
                "ORG_B_APP_API_KEY to test cross-tenant access to app data via the public API."
            ),
            "type": "bearer_token",
            "secret": f"Bearer {org_a['app_api_token']}",
        }
    )
    creds.append(
        {
            "credential_id": "ORG_B_APP_API_KEY",
            "description": (
                f"Public Service API key for Org B's seeded chat app (app_id {org_b['app_id']}, tenant "
                f"{tenant_b['id']}). Same shape as ORG_A_APP_API_KEY. Pair with it for cross-tenant "
                "public-API isolation tests."
            ),
            "type": "bearer_token",
            "secret": f"Bearer {org_b['app_api_token']}",
        }
    )
    creds.append(
        {
            "credential_id": "ORG_A_DATASET_API_KEY",
            "description": (
                f"Tenant-scoped dataset (knowledge base) API key for Org A (tenant {tenant_a['id']}, "
                f"dataset {org_a['dataset_id']}). Send as 'Authorization: Bearer <token>' against "
                "/v1/datasets/* endpoints. This key is tenant-wide (not single-dataset scoped) -- use it "
                f"to check whether it can reach datasets outside {org_a['dataset_id']} within tenant A, "
                "and pair with ORG_B_DATASET_API_KEY for cross-tenant dataset isolation."
            ),
            "type": "bearer_token",
            "secret": f"Bearer {org_a['dataset_api_token']}",
        }
    )
    creds.append(
        {
            "credential_id": "ORG_B_DATASET_API_KEY",
            "description": (
                f"Tenant-scoped dataset API key for Org B (tenant {tenant_b['id']}, dataset "
                f"{org_b['dataset_id']}). Same shape as ORG_A_DATASET_API_KEY. Pair with it for "
                "cross-tenant dataset-API isolation tests."
            ),
            "type": "bearer_token",
            "secret": f"Bearer {org_b['dataset_api_token']}",
        }
    )

    credentials_yaml = ["credentials:"]
    for c in creds:
        credentials_yaml.append(f"  - credential_id: {c['credential_id']}")
        desc = c["description"].replace('"', '\\"')
        credentials_yaml.append(f'    description: "{desc}"')
        credentials_yaml.append(f"    type: {c['type']}")
        if "identifier" in c:
            credentials_yaml.append(f'    identifier: "{c["identifier"]}"')
        secret = c["secret"].replace('"', '\\"')
        credentials_yaml.append(f'    secret: "{secret}"')

    fixtures = [
        {
            "name": "tenant_a",
            "description": "Org A tenant/workspace id and name. Use when a request body or path needs an explicit tenant/workspace identifier for Org A.",
            "value": {"id": tenant_a["id"], "name": tenant_a["name"]},
        },
        {
            "name": "tenant_b",
            "description": "Org B tenant/workspace id and name. Separate tenant from Org A, used for cross-tenant isolation tests.",
            "value": {"id": tenant_b["id"], "name": tenant_b["name"]},
        },
        {
            "name": "org_a_chat_app",
            "description": "Seeded chat app in Org A, owned by OWNER_A. Use for app-CRUD, app-settings, and app-scoped authorization tests, and as the app_id path segment for its ORG_A_APP_API_KEY.",
            "value": {"app_id": org_a["app_id"], "tenant_id": tenant_a["id"], "mode": "chat"},
        },
        {
            "name": "org_b_chat_app",
            "description": "Seeded chat app in Org B, owned by OWNER_B. Cross-tenant counterpart to org_a_chat_app.",
            "value": {"app_id": org_b["app_id"], "tenant_id": tenant_b["id"], "mode": "chat"},
        },
        {
            "name": "org_a_dataset",
            "description": "Seeded knowledge-base dataset in Org A, owned by OWNER_A, indexing_technique=economy (no embedding model configured -- keyword index only, no documents processed through a real embedding pipeline). Use for dataset-CRUD, document-management, and dataset-scoped authorization tests.",
            "value": {"dataset_id": org_a["dataset_id"], "tenant_id": tenant_a["id"], "indexing_technique": "economy"},
        },
        {
            "name": "org_b_dataset",
            "description": "Seeded knowledge-base dataset in Org B, owned by OWNER_B. Cross-tenant counterpart to org_a_dataset.",
            "value": {"dataset_id": org_b["dataset_id"], "tenant_id": tenant_b["id"], "indexing_technique": "economy"},
        },
        {
            "name": "console_auth_mechanics",
            "description": (
                "Full console session auth recipe (each username_password credential's description "
                "only has room for the summary -- this is the complete, live-verified version). "
                "1) POST /console/api/login, JSON body {email, password}. 'password' must be the raw "
                "password run through standard base64 (e.g. base64.b64encode(raw.encode()).decode()) "
                "before sending -- controllers/console/wraps.py decrypt_password_field / "
                "libs/encryption.py FieldEncryption.decrypt_field despite the name just base64-decodes "
                "it, it is NOT real encryption, but sending plaintext still 401s with "
                "{\"code\":\"authentication_failed\",\"message\":\"Invalid encrypted data\"}. "
                "2) On success (200 {\"result\":\"success\"}) the response has NO tokens in the JSON "
                "body -- it sets three Set-Cookie headers instead: access_token (httponly, ~1h TTL), "
                "refresh_token (httponly, 30d TTL), csrf_token (NOT httponly, same TTL as access_token). "
                "3) Every authenticated console endpoint (GET included, not just mutating verbs) calls "
                "libs/token.py check_csrf_token on each request: it 401s ('CSRF token is missing or "
                "invalid') unless header 'X-CSRF-Token' is present AND exactly equals the csrf_token "
                "cookie value. Cookies alone (access_token+refresh_token, no X-CSRF-Token header) are "
                "NOT enough. A bare 'Authorization: Bearer <access_token>' with no cookies at all is "
                "also not enough -- the code path that accepts a bearer token still runs the same "
                "csrf_token-cookie-vs-header comparison, so no cookies means no csrf_token to compare "
                "against and it fails. Live-verified against this harness: base64-password login -> 200 "
                "+ 3 cookies; GET /console/api/apps with cookies + matching X-CSRF-Token -> 200 with app "
                "data; identical GET with the cookies but WITHOUT the X-CSRF-Token header -> 401. "
                "4) Refresh via POST /console/api/refresh-token (cookie-based) before the access_token "
                "TTL expires; logout via POST /console/api/logout. "
                "5) The public Service API (/v1/*, used by ORG_*_APP_API_KEY / ORG_*_DATASET_API_KEY) is "
                "unrelated to all of the above -- it is a plain 'Authorization: Bearer <token>' bearer "
                "scheme with no cookies and no CSRF check."
            ),
            "value": None,
        },
        {
            "name": "llm_model_provider",
            "description": "No real LLM/embedding provider is configured in this harness (no API keys for OpenAI/Anthropic/etc). Apps and datasets exist and are fully reachable for CRUD/authorization testing, but actually *running* a chat completion, workflow LLM node, or high-quality (embedding-based) dataset indexing will fail with a provider-not-configured error. This is an accepted coverage gap, not an app bug -- see accepted-coverage-gaps.yaml.",
            "value": None,
        },
    ]

    fixtures_yaml = ["fixtures:"]
    for f in fixtures:
        fixtures_yaml.append(f"  - name: {f['name']}")
        desc = f["description"].replace('"', '\\"')
        fixtures_yaml.append(f'    description: "{desc}"')
        if f["value"] is None:
            fixtures_yaml.append("    value: null")
        else:
            fixtures_yaml.append("    value:")
            for k, v in f["value"].items():
                fixtures_yaml.append(f'      {k}: "{v}"')

    repo_root = Path(__file__).resolve().parents[2]
    niro_dir = repo_root / "niro"
    (niro_dir / "credentials.yaml").write_text(
        "# yaml-language-server: $schema=https://niro.apxlabs.ai/schema/v1/credentials.json\n"
        "# GENERATED by niro/harness/seed.sh + render_niro_files.py. Do not edit by hand --\n"
        "# re-run niro/harness/seed.sh. Local-only, git-ignored.\n\n" + "\n".join(credentials_yaml) + "\n"
    )
    (niro_dir / "fixtures.yaml").write_text(
        "# yaml-language-server: $schema=https://niro.apxlabs.ai/schema/v1/fixtures.json\n"
        "# GENERATED by niro/harness/seed.sh + render_niro_files.py. Do not edit by hand --\n"
        "# re-run niro/harness/seed.sh. Local-only, git-ignored.\n\n" + "\n".join(fixtures_yaml) + "\n"
    )
    print("Wrote niro/credentials.yaml and niro/fixtures.yaml", file=sys.stderr)


if __name__ == "__main__":
    main()

"""Niro harness seed script.

Runs INSIDE the `api` container (copied in by seed.sh and executed with
`python3`). Creates a deterministic baseline of tenants, accounts (across
every TenantAccountRole), API tokens, an app, and a dataset for the Niro
pentest sweep to exercise.

Uses the application's own service layer (RegisterService, TenantService,
AppService, DatasetService) rather than raw SQL so the created rows are
exactly what the normal signup/console flows would produce. Deliberately
avoids requiring a configured LLM/embedding provider: apps are created with
their default (unconfigured) model config, and the dataset uses "economy"
indexing so no embedding model is required.

Prints ONE line of JSON between the NIRO_SEED_JSON_START/END markers on
stdout; everything else the app logs during import goes to stderr or is
irrelevant. seed.sh parses stdout for that marker pair.

Idempotent: safe to re-run against a fresh (or already-seeded) database.
Re-running after a `reset.sh` (which wipes the postgres volume) starts
from an empty DB again, so this always runs the "first boot" path.
"""

from __future__ import annotations

import json
import sys

from app_factory import create_app
from extensions.ext_database import db
from models.account import TenantAccountRole
from models.enums import ApiTokenType
from models.model import ApiToken
from services.account_service import RegisterService, TenantService
from services.app_service import AppService, CreateAppParams
from services.dataset_service import DatasetService

PASSWORD_SUFFIX = "-Niro26!"  # satisfies password_pattern: letters + digits, len >= 8


def make_password(role_tag: str) -> str:
    return f"{role_tag}{PASSWORD_SUFFIX}"


def create_app_token(app_id: str, tenant_id: str, session) -> str:
    # Idempotent: BaseApiKeyListResource (the real console endpoint) caps
    # keys per resource at 10 and direct-inserting here bypasses that cap
    # entirely, so re-running the seed script must reuse an existing token
    # rather than minting an unbounded number of throwaway ones.
    existing = session.query(ApiToken).filter(ApiToken.app_id == app_id, ApiToken.type == ApiTokenType.APP).first()
    if existing is not None:
        return existing.token
    token = ApiToken.generate_api_key("app-", 24)
    api_token = ApiToken()
    api_token.tenant_id = tenant_id
    api_token.app_id = app_id
    api_token.token = token
    api_token.type = ApiTokenType.APP
    session.add(api_token)
    session.commit()
    return token


def create_dataset_token(tenant_id: str, session) -> str:
    # Idempotent for the same reason as create_app_token. Dataset tokens are
    # tenant-scoped (not per-dataset), so reuse keys off tenant_id alone.
    existing = (
        session.query(ApiToken).filter(ApiToken.tenant_id == tenant_id, ApiToken.type == ApiTokenType.DATASET).first()
    )
    if existing is not None:
        return existing.token
    token = ApiToken.generate_api_key("dataset-", 24)
    api_token = ApiToken()
    api_token.tenant_id = tenant_id
    api_token.token = token
    api_token.type = ApiTokenType.DATASET
    session.add(api_token)
    session.commit()
    return token


def main() -> None:
    _socketio_app, flask_app = create_app()
    result: dict[str, object] = {}

    with flask_app.app_context():
        session = db.session()

        # --- DifySetup bootstrap: the very first account/tenant must go
        # through RegisterService.setup() -- it is what marks the instance
        # as "set up" (writes the DifySetup row). Every console login is
        # blocked with NotSetupError until that row exists.
        existing = None
        from models.account import Account

        existing = session.query(Account).filter(Account.email == "owner-a@niro.test").first()

        if existing is None:
            owner_a_password = make_password("OwnerA")
            RegisterService.setup(
                email="owner-a@niro.test",
                name="Niro Owner A",
                password=owner_a_password,
                ip_address="127.0.0.1",
                language="en-US",
                session=session,
            )
            owner_a = session.query(Account).filter(Account.email == "owner-a@niro.test").one()
            tenant_a = TenantService.get_join_tenants(owner_a, session=session)[0]
            tenant_a.name = "Niro Org A"
            session.commit()
        else:
            owner_a = existing
            owner_a_password = make_password("OwnerA")
            tenant_a = TenantService.get_join_tenants(owner_a, session=session)[0]

        def ensure_account(email: str, name: str, role_tag: str) -> tuple["Account", str]:
            acct = session.query(Account).filter(Account.email == email).first()
            password = make_password(role_tag)
            if acct is None:
                acct = RegisterService.register(
                    email=email,
                    name=name,
                    password=password,
                    language="en-US",
                    create_workspace_required=False,
                    session=session,
                )
            return acct, password

        from models.account import TenantAccountJoin

        def ensure_member(tenant, account, role: TenantAccountRole) -> None:
            # create_tenant_member() unconditionally rejects a second OWNER
            # role assignment on a tenant that already has one -- even a
            # no-op re-assignment to the SAME account -- so re-running this
            # script against an already-seeded DB must skip membership rows
            # that already exist rather than calling create_tenant_member again.
            existing = (
                session.query(TenantAccountJoin)
                .filter(TenantAccountJoin.tenant_id == tenant.id, TenantAccountJoin.account_id == account.id)
                .first()
            )
            if existing is not None:
                return
            TenantService.create_tenant_member(tenant, account, session, role=role)

        # --- Org A: full role matrix on the same tenant ---
        admin_a, admin_a_password = ensure_account("admin-a@niro.test", "Niro Admin A", "AdminA")
        ensure_member(tenant_a, admin_a, TenantAccountRole.ADMIN)

        member_a1, member_a1_password = ensure_account("member-a1@niro.test", "Niro Member A1", "MemberA1")
        ensure_member(tenant_a, member_a1, TenantAccountRole.NORMAL)

        member_a2, member_a2_password = ensure_account("member-a2@niro.test", "Niro Member A2", "MemberA2")
        ensure_member(tenant_a, member_a2, TenantAccountRole.NORMAL)

        editor_a, editor_a_password = ensure_account("editor-a@niro.test", "Niro Editor A", "EditorA")
        ensure_member(tenant_a, editor_a, TenantAccountRole.EDITOR)

        operator_a, operator_a_password = ensure_account(
            "operator-a@niro.test", "Niro Dataset Operator A", "OperatorA"
        )
        ensure_member(tenant_a, operator_a, TenantAccountRole.DATASET_OPERATOR)

        # --- Org B: separate tenant, separate owner, for cross-tenant
        # isolation testing. Bootstrapped with is_setup=True to bypass the
        # ALLOW_CREATE_WORKSPACE gate at the service layer -- this is our
        # own seed script acting with elevated (DB-level) trust, not a
        # simulated attacker path.
        owner_b, owner_b_password = ensure_account("owner-b@niro.test", "Niro Owner B", "OwnerB")
        tenant_b = session.query(type(tenant_a)).filter(type(tenant_a).name == "Niro Org B").first()
        if tenant_b is None:
            tenant_b = TenantService.create_tenant(name="Niro Org B", is_setup=True, session=session)
        ensure_member(tenant_b, owner_b, TenantAccountRole.OWNER)

        member_b1, member_b1_password = ensure_account("member-b1@niro.test", "Niro Member B1", "MemberB1")
        ensure_member(tenant_b, member_b1, TenantAccountRole.NORMAL)

        session.commit()

        # --- App + dataset + API tokens per org (owned by that org's owner) ---
        def seed_org_resources(tenant, owner_account, tag: str) -> dict[str, str]:
            TenantService.switch_tenant(owner_account, tenant_id=tenant.id, session=session)

            existing_app = None
            from models.model import App as AppModel

            existing_app = (
                session.query(AppModel)
                .filter(AppModel.tenant_id == tenant.id, AppModel.name == f"Niro {tag} Chat App")
                .first()
            )
            if existing_app is None:
                app = AppService().create_app(
                    tenant_id=tenant.id,
                    params=CreateAppParams(
                        name=f"Niro {tag} Chat App",
                        description=f"Seeded chat app owned by {tag}'s org for the Niro pentest sweep.",
                        mode="chat",
                    ),
                    account=owner_account,
                    session=session,
                )
                session.commit()
            else:
                app = existing_app

            app_api_token = create_app_token(app.id, tenant.id, session)

            from models.dataset import Dataset

            existing_dataset = (
                session.query(Dataset)
                .filter(Dataset.tenant_id == tenant.id, Dataset.name == f"Niro {tag} Knowledge Base")
                .first()
            )
            if existing_dataset is None:
                dataset = DatasetService.create_empty_dataset(
                    tenant_id=tenant.id,
                    name=f"Niro {tag} Knowledge Base",
                    description=f"Seeded dataset owned by {tag}'s org for RAG/knowledge-base testing.",
                    indexing_technique="economy",
                    account=owner_account,
                    session=session,
                )
                session.commit()
            else:
                dataset = existing_dataset

            dataset_api_token = create_dataset_token(tenant.id, session)

            return {
                "app_id": app.id,
                "app_api_token": app_api_token,
                "dataset_id": dataset.id,
                "dataset_api_token": dataset_api_token,
            }

        org_a_resources = seed_org_resources(tenant_a, owner_a, "OrgA")
        org_b_resources = seed_org_resources(tenant_b, owner_b, "OrgB")

        result = {
            "tenant_a": {"id": tenant_a.id, "name": tenant_a.name},
            "tenant_b": {"id": tenant_b.id, "name": tenant_b.name},
            "accounts": {
                "owner_a": {"email": "owner-a@niro.test", "password": owner_a_password, "role": "owner", "tenant": "A"},
                "admin_a": {"email": "admin-a@niro.test", "password": admin_a_password, "role": "admin", "tenant": "A"},
                "editor_a": {
                    "email": "editor-a@niro.test",
                    "password": editor_a_password,
                    "role": "editor",
                    "tenant": "A",
                },
                "member_a1": {
                    "email": "member-a1@niro.test",
                    "password": member_a1_password,
                    "role": "normal",
                    "tenant": "A",
                },
                "member_a2": {
                    "email": "member-a2@niro.test",
                    "password": member_a2_password,
                    "role": "normal",
                    "tenant": "A",
                },
                "operator_a": {
                    "email": "operator-a@niro.test",
                    "password": operator_a_password,
                    "role": "dataset_operator",
                    "tenant": "A",
                },
                "owner_b": {"email": "owner-b@niro.test", "password": owner_b_password, "role": "owner", "tenant": "B"},
                "member_b1": {
                    "email": "member-b1@niro.test",
                    "password": member_b1_password,
                    "role": "normal",
                    "tenant": "B",
                },
            },
            "org_a": org_a_resources,
            "org_b": org_b_resources,
        }

    print("===NIRO_SEED_JSON_START===")
    print(json.dumps(result))
    print("===NIRO_SEED_JSON_END===")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - surface any failure clearly to seed.sh
        print(f"NIRO_SEED_FAILED: {exc}", file=sys.stderr)
        raise

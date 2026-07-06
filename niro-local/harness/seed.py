from __future__ import annotations

import base64
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import sqlalchemy as sa

from configs import dify_config
from extensions.ext_database import db
from extensions.storage.storage_type import StorageType
from libs.datetime_utils import naive_utc_now
from libs.password import hash_password, valid_password
from libs.rsa import generate_key_pair
from models import Account, Tenant, TenantAccountJoin, TenantAccountRole
from models.dataset import Dataset, DatasetPermissionEnum
from models.enums import ApiTokenType, CreatorUserRole, CustomizeTokenStrategy, DataSourceType
from models.model import ApiToken, App, AppMode, AppStatus, DifySetup, IconType, Site, UploadFile

PASSWORD = "Niro-Local-Password-2026!"
NIRO_DIR = Path(os.environ.get("NIRO_DIR", "/niro")).resolve()
ROOT = NIRO_DIR / "harness"

ACTORS = [
    ("owner_a@niro.local", "Niro Owner A", "tenant_a", TenantAccountRole.OWNER),
    ("admin_a@niro.local", "Niro Admin A", "tenant_a", TenantAccountRole.ADMIN),
    ("editor_a@niro.local", "Niro Editor A", "tenant_a", TenantAccountRole.EDITOR),
    ("member_a@niro.local", "Niro Member A", "tenant_a", TenantAccountRole.NORMAL),
    ("member_b@niro.local", "Niro Member B", "tenant_b", TenantAccountRole.NORMAL),
    ("owner_b@niro.local", "Niro Owner B", "tenant_b", TenantAccountRole.OWNER),
]


def _stable_uuid(name: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"https://dify.local/niro/{name}")


def _stable_id(name: str) -> str:
    return str(_stable_uuid(name))


def _account(email: str, name: str) -> Account:
    account_id = _stable_id(f"account/{email}")
    existing = db.session.scalar(sa.select(Account).where(sa.or_(Account.id == account_id, Account.email == email)))
    if existing:
        return existing
    valid_password(PASSWORD)
    salt = _stable_uuid(f"account-salt/{email}").bytes
    account = Account(
        name=name,
        email=email,
        password=base64.b64encode(hash_password(PASSWORD, salt)).decode(),
        password_salt=base64.b64encode(salt).decode(),
        interface_language="en-US",
        interface_theme="light",
        timezone="UTC",
    )
    account.id = account_id
    db.session.add(account)
    return account


def _tenant(key: str, name: str) -> Tenant:
    tenant_id = _stable_id(f"tenant/{key}")
    existing = db.session.scalar(sa.select(Tenant).where(sa.or_(Tenant.id == tenant_id, Tenant.name == name)))
    if existing:
        return existing
    tenant = Tenant(name=name, encrypt_public_key=generate_key_pair(tenant_id))
    tenant.id = tenant_id
    db.session.add(tenant)
    return tenant


def _join(tenant: Tenant, account: Account, role: TenantAccountRole, current: bool) -> None:
    join_id = _stable_id(f"tenant-account-join/{tenant.id}/{account.id}")
    existing = db.session.scalar(
        sa.select(TenantAccountJoin).where(
            sa.or_(
                TenantAccountJoin.id == join_id,
                sa.and_(TenantAccountJoin.tenant_id == tenant.id, TenantAccountJoin.account_id == account.id),
            )
        )
    )
    if existing:
        existing.role = role
        existing.current = current
        return
    join = TenantAccountJoin(
        tenant_id=tenant.id,
        account_id=account.id,
        role=role,
        current=current,
    )
    join.id = join_id
    db.session.add(join)


def _app(tenant: Tenant, account: Account, name: str) -> App:
    app_id = _stable_id(f"app/{tenant.id}/{name}")
    existing = db.session.scalar(sa.select(App).where(sa.or_(App.id == app_id, sa.and_(App.tenant_id == tenant.id, App.name == name))))
    if existing:
        return existing
    app = App(
        id=app_id,
        tenant_id=tenant.id,
        name=name,
        description=f"Deterministic Niro app for {tenant.name}",
        mode=AppMode.CHAT,
        icon_type=IconType.EMOJI,
        icon="shield",
        icon_background="#E5E7EB",
        status=AppStatus.NORMAL,
        enable_site=True,
        enable_api=True,
        api_rpm=0,
        api_rph=0,
        is_demo=False,
        is_public=False,
        is_universal=False,
        max_active_requests=None,
        created_by=account.id,
        maintainer=account.id,
        updated_by=account.id,
    )
    db.session.add(app)
    db.session.flush()
    db.session.add(
        Site(
            id=_stable_id(f"site/{app_id}"),
            app_id=app.id,
            title=name,
            icon_type=IconType.EMOJI,
            icon="shield",
            icon_background="#E5E7EB",
            description=f"Public web app surface for {name}",
            default_language="en-US",
            chat_color_theme="#2563EB",
            chat_color_theme_inverted=False,
            copyright="",
            privacy_policy="",
            input_placeholder="",
            show_workflow_steps=True,
            use_icon_as_answer_icon=False,
            customize_domain="",
            customize_token_strategy=CustomizeTokenStrategy.NOT_ALLOW,
            prompt_public=False,
            status=AppStatus.NORMAL,
            created_by=account.id,
            updated_by=account.id,
            code=_stable_id(f"site-code/{app_id}").replace("-", "")[:16],
        )
    )
    return app


def _dataset(tenant: Tenant, account: Account, name: str, permission: DatasetPermissionEnum) -> Dataset:
    dataset_id = _stable_id(f"dataset/{tenant.id}/{name}")
    existing = db.session.scalar(
        sa.select(Dataset).where(sa.or_(Dataset.id == dataset_id, sa.and_(Dataset.tenant_id == tenant.id, Dataset.name == name)))
    )
    if existing:
        return existing
    dataset = Dataset(
        id=dataset_id,
        tenant_id=tenant.id,
        name=name,
        description=f"Deterministic Niro dataset for {tenant.name}",
        provider="vendor",
        permission=permission,
        data_source_type=DataSourceType.UPLOAD_FILE,
        indexing_technique=None,
        created_by=account.id,
        maintainer=account.id,
        updated_by=account.id,
        enable_api=True,
    )
    db.session.add(dataset)
    return dataset


def _api_token(app: App, tenant: Tenant) -> ApiToken:
    token_id = _stable_id(f"api-token/{app.id}")
    existing = db.session.scalar(
        sa.select(ApiToken).where(sa.or_(ApiToken.id == token_id, sa.and_(ApiToken.app_id == app.id, ApiToken.type == ApiTokenType.APP)))
    )
    if existing:
        return existing
    token = ApiToken(
        id=token_id,
        app_id=app.id,
        tenant_id=tenant.id,
        type=ApiTokenType.APP,
        token=f"app-niro-{_stable_id(f'api-token-value/{app.id}').replace('-', '')[:32]}",
    )
    db.session.add(token)
    return token


def _upload(tenant: Tenant, account: Account, name: str) -> UploadFile:
    upload_id = _stable_id(f"upload/{tenant.id}/{name}")
    existing = db.session.scalar(
        sa.select(UploadFile).where(sa.or_(UploadFile.id == upload_id, sa.and_(UploadFile.tenant_id == tenant.id, UploadFile.name == name)))
    )
    if existing:
        return existing
    upload = UploadFile(
        tenant_id=tenant.id,
        storage_type=StorageType.OPENDAL,
        key=f"niro/{tenant.id}/{name}",
        name=name,
        size=128,
        extension=".txt",
        mime_type="text/plain",
        created_by_role=CreatorUserRole.ACCOUNT,
        created_by=account.id,
        created_at=naive_utc_now(),
        used=False,
    )
    upload.id = upload_id
    db.session.add(upload)
    return upload


def _setup_marker() -> None:
    if db.session.scalar(sa.select(DifySetup).limit(1)):
        return
    db.session.add(DifySetup(version=dify_config.project.version))


def _write_yaml(path: Path, data: str) -> None:
    path.write_text(data, encoding="utf-8")


def _credential_yaml(accounts: dict[str, Account]) -> str:
    lines = ["credentials:"]
    for email, account in accounts.items():
        lines.extend(
            [
                f'  - description: "{email}. Login: POST /console/api/login with JSON body '
                f"{{email, password:<base64 secret>, remember_me:false}}. Account id {account.id}; "
                "roles/resources are enumerated in fixtures.yaml for horizontal and vertical authz testing.\"",
                "    type: username_password",
                f"    identifier: {email}",
                f"    secret: {PASSWORD}",
            ]
        )
    return "\n".join(lines) + "\n"


def _fixtures_yaml(payload: dict[str, Any]) -> str:
    import json

    return "fixtures:\n" + "\n".join(
        [
            f"  - name: {name}\n"
            f"    description: \"{description}\"\n"
            f"    value: {json.dumps(value, sort_keys=True)}"
            for name, description, value in payload["fixtures"]
        ]
    ) + "\n"


def seed() -> None:
    _setup_marker()
    tenants = {
        "tenant_a": _tenant("tenant_a", "Niro Tenant A"),
        "tenant_b": _tenant("tenant_b", "Niro Tenant B"),
    }
    accounts = {email: _account(email, name) for email, name, _, _ in ACTORS}
    for email, _, tenant_key, role in ACTORS:
        _join(tenants[tenant_key], accounts[email], role, current=True)

    owner_a = accounts["owner_a@niro.local"]
    owner_b = accounts["owner_b@niro.local"]
    app_a = _app(tenants["tenant_a"], owner_a, "Niro Chat App A")
    app_b = _app(tenants["tenant_b"], owner_b, "Niro Chat App B")
    dataset_a = _dataset(tenants["tenant_a"], owner_a, "Niro Private Dataset A", DatasetPermissionEnum.ONLY_ME)
    dataset_b = _dataset(tenants["tenant_b"], owner_b, "Niro Private Dataset B", DatasetPermissionEnum.ONLY_ME)
    token_a = _api_token(app_a, tenants["tenant_a"])
    token_b = _api_token(app_b, tenants["tenant_b"])
    upload_a = _upload(tenants["tenant_a"], owner_a, "niro-a.txt")
    upload_b = _upload(tenants["tenant_b"], owner_b, "niro-b.txt")
    db.session.commit()

    fixture_items = [
        ("target", "Local Dify target roots owned by the Niro harness.", {
            "web_url": "http://host.docker.internal:3000",
            "api_url": "http://host.docker.internal:5001",
            "host_web_url": "http://127.0.0.1:3000",
            "host_api_url": "http://127.0.0.1:5001",
            "console_login_path": "/console/api/login",
            "console_login_password_encoding": "base64",
            "public_api_prefix": "/v1",
        }),
        ("tenants", "Two isolated workspaces for horizontal tenant-isolation tests.", {
            key: {"id": tenant.id, "name": tenant.name} for key, tenant in tenants.items()
        }),
        ("accounts", "Seeded accounts, roles, and workspace memberships.", {
            email: {"id": account.id, "name": account.name} for email, account in accounts.items()
        }),
        ("apps", "One API-enabled chat app per tenant.", {
            "tenant_a_app": {"id": app_a.id, "tenant_id": app_a.tenant_id, "api_token": token_a.token},
            "tenant_b_app": {"id": app_b.id, "tenant_id": app_b.tenant_id, "api_token": token_b.token},
        }),
        ("datasets", "One private dataset per tenant for cross-tenant and owner-only tests.", {
            "tenant_a_dataset": {"id": dataset_a.id, "tenant_id": dataset_a.tenant_id, "permission": str(dataset_a.permission)},
            "tenant_b_dataset": {"id": dataset_b.id, "tenant_id": dataset_b.tenant_id, "permission": str(dataset_b.permission)},
        }),
        ("uploads", "Upload metadata rows for file/input handling and ownership tests.", {
            "tenant_a_upload": {"id": upload_a.id, "tenant_id": upload_a.tenant_id, "name": upload_a.name},
            "tenant_b_upload": {"id": upload_b.id, "tenant_id": upload_b.tenant_id, "name": upload_b.name},
        }),
        ("code_version", "Git revision baked into the locally built API/web images.", {
            "commit_sha": os.environ.get("COMMIT_SHA", "unknown"),
            "seeded_at_utc": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        }),
    ]

    _write_yaml(NIRO_DIR / "credentials.yaml", _credential_yaml(accounts))
    _write_yaml(NIRO_DIR / "fixtures.yaml", _fixtures_yaml({"fixtures": fixture_items}))


if __name__ == "__main__":
    from app_factory import create_app

    _, flask_app = create_app()
    with flask_app.app_context():
        seed()

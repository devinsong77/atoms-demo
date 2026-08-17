import hashlib
import hmac
import os
import re

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import sessionmaker

from .config import settings
from .models import Base, Project, ProjectTenant, Run, User


engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def tenant_schema_name(project_id: str) -> str:
    schema_name = f"tenant_{project_id.replace('-', '')}"
    if not re.fullmatch(r"tenant_[a-f0-9]{32}", schema_name):
        raise ValueError("Invalid project id for tenant schema")
    return schema_name


def ensure_tenant_schema(db: DbSession, schema_name: str) -> None:
    if not re.fullmatch(r"tenant_[a-f0-9]{32}", schema_name):
        raise ValueError("Invalid tenant schema name")
    # The identifier is safe after the strict allow-list validation above.
    db.connection().exec_driver_sql(f'CREATE SCHEMA IF NOT EXISTS "{schema_name}"')


def password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 240_000)
    return f"pbkdf2_sha256${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        _, salt_hex, digest_hex = encoded.split("$", 2)
        candidate = password_hash(password, bytes.fromhex(salt_hex)).split("$")[-1]
        return hmac.compare_digest(candidate, digest_hex)
    except (ValueError, TypeError):
        return False


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE user_preferences ADD COLUMN IF NOT EXISTS locale VARCHAR(8) NOT NULL DEFAULT 'zh-CN'")
        connection.exec_driver_sql("ALTER TABLE user_preferences ADD COLUMN IF NOT EXISTS theme VARCHAR(12) NOT NULL DEFAULT 'light'")
        connection.exec_driver_sql("ALTER TABLE deployments ADD COLUMN IF NOT EXISTS error TEXT")
        connection.exec_driver_sql("ALTER TABLE deployments ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP")
        connection.exec_driver_sql("ALTER TABLE projects ADD COLUMN IF NOT EXISTS icon_data_url TEXT")
        connection.exec_driver_sql("ALTER TABLE projects ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ")
        connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_projects_archived_at ON projects (archived_at)")
    accounts = [
        ("demo@atoms.local", "Demo Builder", "demo123", "#7c5cff"),
        ("founder@atoms.local", "Startup Founder", "atoms123", "#12b981"),
        ("builder@atoms.local", "Product Builder", "build123", "#f59e0b"),
    ]
    with SessionLocal() as db:
        interrupted = db.scalars(
            select(Run).where(Run.state.in_(["created", "planning", "building", "testing"]))
        ).all()
        for run in interrupted:
            run.state = "failed"
            run.error = "控制服务重启，本轮已安全终止，请重新发送指令"
            project = db.get(Project, run.project_id)
            if project:
                project.status = "failed"
        for project in db.scalars(select(Project)).all():
            tenant = db.get(ProjectTenant, project.id)
            if not tenant:
                schema_name = tenant_schema_name(project.id)
                db.add(ProjectTenant(project_id=project.id, schema_name=schema_name))
            else:
                schema_name = tenant.schema_name
            ensure_tenant_schema(db, schema_name)
        for email, name, password, color in accounts:
            if not db.scalar(select(User).where(User.email == email)):
                db.add(User(email=email, name=name, password_hash=password_hash(password), avatar_color=color))
        db.commit()


def session_scope() -> DbSession:
    return SessionLocal()

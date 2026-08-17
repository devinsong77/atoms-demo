import asyncio
import base64
import binascii
import json
import shutil
import uuid
import zipfile
import mimetypes
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
import black
import websockets
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from .agent import begin_run, run_builder
from .auth import create_session, current_user, event_user, user_from_token
from .config import settings
from .db import SessionLocal, ensure_tenant_schema, get_db, init_db, tenant_schema_name
from .deployments import CloudProviderError, deployment_provider
from .models import AgentEvent, Deployment, Project, ProjectTenant, Run, User, UserPreference, Version
from .preview import preview_manager
from .schemas import CloudContainerAction, CloudExecRequest, FileUpdate, LoginRequest, MessageCreate, ModeUpdate, ProjectCreate, SettingsUpdate
from .workspace import ensure_workspace, list_files, project_root, run_checks, safe_path


ALLOWED_ATTACHMENT_MIMES = {
    "image/png", "image/jpeg", "image/webp", "image/gif", "application/pdf",
    "text/plain", "text/markdown", "text/csv", "application/json",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def save_attachments(project_id: str, attachments) -> list[dict]:
    stored = []
    folder = ensure_workspace(project_id) / ".atoms" / "attachments"
    folder.mkdir(parents=True, exist_ok=True)
    for item in attachments:
        if item.mime not in ALLOWED_ATTACHMENT_MIMES:
            raise HTTPException(status_code=415, detail=f"不支持的附件类型: {item.mime}")
        match = re.fullmatch(r"data:([^;,]+);base64,(.+)", item.data_url, re.DOTALL)
        if not match or match.group(1) != item.mime:
            raise HTTPException(status_code=422, detail=f"附件编码无效: {item.name}")
        try:
            raw = base64.b64decode(match.group(2), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(status_code=422, detail=f"附件编码无效: {item.name}") from exc
        if len(raw) > 8 * 1024 * 1024:
            raise HTTPException(status_code=413, detail=f"附件超过 8MB: {item.name}")
        attachment_id = uuid.uuid4().hex
        suffix = Path(item.name).suffix[:12] or mimetypes.guess_extension(item.mime) or ".bin"
        target = folder / f"{attachment_id}{suffix}"
        target.write_bytes(raw)
        stored.append({"id": attachment_id, "name": Path(item.name).name, "mime": item.mime, "size": len(raw), "path": target.relative_to(ensure_workspace(project_id)).as_posix()})
    return stored


@asynccontextmanager
async def lifespan(_: FastAPI):
    for attempt in range(20):
        try:
            init_db()
            break
        except Exception:
            if attempt == 19:
                raise
            await asyncio.sleep(1)
    yield
    preview_manager.stop_all()


app = FastAPI(title="Atoms Demo API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000", settings.public_app_url],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(CloudProviderError)
async def cloud_provider_error(_: Request, exc: CloudProviderError):
    return JSONResponse(status_code=502, content={"detail": str(exc)})


def project_for_user(db: DbSession, project_id: str, user: User) -> Project:
    project = db.scalar(select(Project).where(Project.id == project_id, Project.owner_id == user.id))
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    return project


def event_dict(event: AgentEvent) -> dict:
    return {
        "id": event.id,
        "run_id": event.run_id,
        "seq": event.seq,
        "agent": event.agent,
        "type": event.type,
        "phase": event.phase,
        "summary": event.public_summary,
        "payload": event.payload,
        "created_at": event.created_at.isoformat(),
    }


def run_dict(run: Run) -> dict:
    return {
        "id": run.id,
        "project_id": run.project_id,
        "goal": run.goal,
        "state": run.state,
        "mode": run.mode,
        "plan": run.plan,
        "error": run.error,
        "created_at": run.created_at.isoformat(),
        "updated_at": run.updated_at.isoformat(),
    }


def project_dict(db: DbSession, project: Project) -> dict:
    latest_run = db.scalar(
        select(Run).where(Run.project_id == project.id).order_by(Run.created_at.desc()).limit(1)
    )
    return {
        "id": project.id,
        "name": project.name,
        "description": project.description,
        "mode": project.mode,
        "status": project.status,
        "icon_data_url": project.icon_data_url,
        "archived_at": project.archived_at.isoformat() if project.archived_at else None,
        "preview_ready": bool(project.preview_port),
        "latest_run": run_dict(latest_run) if latest_run else None,
        "created_at": project.created_at.isoformat(),
        "updated_at": project.updated_at.isoformat(),
    }


def validated_project_icon(value: str | None) -> str | None:
    if not value:
        return None
    prefixes = ("data:image/png;base64,", "data:image/jpeg;base64,", "data:image/webp;base64,")
    prefix = next((item for item in prefixes if value.startswith(item)), None)
    if not prefix:
        raise HTTPException(status_code=422, detail="项目图标仅支持 PNG、JPEG 或 WebP")
    try:
        raw = base64.b64decode(value[len(prefix):], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=422, detail="项目图标数据无效") from exc
    if len(raw) > 512_000:
        raise HTTPException(status_code=422, detail="项目图标不能超过 500KB")
    return value


def deployment_dict(row: Deployment) -> dict:
    return {
        "id": row.id,
        "project_id": row.project_id,
        "version_id": row.version_id,
        "provider": row.provider,
        "status": row.status,
        "url": row.url,
        "error": row.error,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


async def cloud_container_for_user(db: DbSession, container_id: str, user: User) -> dict:
    containers = await deployment_provider.containers()
    container = next((item for item in containers if item["id"] == container_id or item["short_id"] == container_id), None)
    if not container:
        raise HTTPException(status_code=404, detail="容器不存在")
    project_for_user(db, container["project_id"], user)
    return container


@app.get("/api/health")
def health():
    return {"status": "ok", "model": settings.rightapi_model, "publish": "atoms-cloud"}


@app.post("/api/auth/login")
def login(body: LoginRequest, db: DbSession = Depends(get_db)):
    token, user = create_session(db, body.email, body.password)
    return {
        "token": token,
        "user": {"id": user.id, "email": user.email, "name": user.name, "avatar_color": user.avatar_color},
    }


@app.get("/api/auth/me")
def me(user: User = Depends(current_user)):
    return {"id": user.id, "email": user.email, "name": user.name, "avatar_color": user.avatar_color}


AI_TEAM = [
    {"id": "mike", "name": "Mike", "role": "Team Leader", "description": "Coordinates plans, approvals and agent handoffs.", "color": "#6247dc", "status": "online"},
    {"id": "emma", "name": "Emma", "role": "Product Manager", "description": "Turns product ideas into scoped, testable plans.", "color": "#e87a9d", "status": "online"},
    {"id": "alex", "name": "Alex", "role": "Full-stack Engineer", "description": "Builds, tests and repairs complete applications.", "color": "#3e8ef7", "status": "online"},
    {"id": "bob", "name": "Bob", "role": "Architect", "description": "Defines system boundaries, API contracts and PostgreSQL tenant data models.", "color": "#e49a3a", "status": "online"},
]

DISCOVER_TEMPLATES = [
    {"id": "feedback", "name": "Feedback Hub", "category": "SaaS", "description": "Idea submission, voting, status workflow and product metrics.", "prompt": "Build a customer feedback hub with PostgreSQL tenant persistence, idea submission, voting, status filters, admin status updates, metrics, and a polished responsive interface.", "accent": "#7c5cff"},
    {"id": "inventory", "name": "Inventory Desk", "category": "Internal tool", "description": "Products, stock movements, alerts and searchable operations table.", "prompt": "Build an inventory dashboard with PostgreSQL tenant persistence, CRUD products, stock adjustments, low-stock alerts, search, summary metrics, and a responsive operations interface.", "accent": "#15a778"},
    {"id": "booking", "name": "Studio Booking", "category": "Marketplace", "description": "Availability, bookings, customer details and schedule management.", "prompt": "Build a studio booking website with PostgreSQL tenant persistence, available slots, booking creation and cancellation, customer details, schedule filters, and a premium responsive design.", "accent": "#e2779b"},
    {"id": "crm", "name": "Pipeline CRM", "category": "Business", "description": "Leads, stages, notes and revenue pipeline overview.", "prompt": "Build a lightweight CRM with PostgreSQL tenant persistence, lead CRUD, pipeline stage updates, notes, search, revenue metrics, and a polished responsive dashboard.", "accent": "#e39a32"},
]


@app.get("/api/team")
def team(user: User = Depends(current_user), db: DbSession = Depends(get_db)):
    members = db.scalars(select(User).order_by(User.created_at)).all()
    return {
        "agents": AI_TEAM,
        "members": [
            {"id": row.id, "name": row.name, "email": row.email, "avatar_color": row.avatar_color, "role": "Owner" if row.id == user.id else "Demo member"}
            for row in members
        ],
    }


@app.get("/api/discover")
def discover(_: User = Depends(current_user)):
    return DISCOVER_TEMPLATES


@app.get("/api/settings")
def get_settings(user: User = Depends(current_user), db: DbSession = Depends(get_db)):
    preference = db.get(UserPreference, user.id)
    if not preference:
        preference = UserPreference(user_id=user.id)
        db.add(preference)
        db.commit()
        db.refresh(preference)
    return {
        "name": user.name,
        "email": user.email,
        "default_mode": preference.default_mode,
        "compact_events": preference.compact_events,
        "locale": preference.locale,
        "theme": preference.theme,
        "model": settings.rightapi_model,
        "api_base": settings.rightapi_base_url,
    }


@app.patch("/api/settings")
def update_settings(
    body: SettingsUpdate,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
):
    user.name = body.name.strip()
    preference = db.get(UserPreference, user.id)
    if not preference:
        preference = UserPreference(user_id=user.id)
        db.add(preference)
    preference.default_mode = body.default_mode
    preference.compact_events = body.compact_events
    preference.locale = body.locale
    preference.theme = body.theme
    db.commit()
    return get_settings(user, db)


@app.get("/api/projects")
def projects(user: User = Depends(current_user), db: DbSession = Depends(get_db)):
    rows = db.scalars(
        select(Project).where(Project.owner_id == user.id, Project.archived_at.is_(None)).order_by(Project.updated_at.desc())
    ).all()
    return [project_dict(db, row) for row in rows]


@app.post("/api/projects", status_code=201)
def create_project(
    body: ProjectCreate,
    background: BackgroundTasks,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
):
    project = Project(
        id=str(uuid.uuid4()),
        owner_id=user.id,
        name=body.name.strip(),
        description=body.description.strip(),
        mode=body.mode,
        status="created",
        icon_data_url=validated_project_icon(body.icon_data_url),
    )
    run = Run(
        id=str(uuid.uuid4()), project_id=project.id, goal=body.description.strip(), mode=body.mode, state="created"
    )
    db.add(project)
    db.add(run)
    db.flush()
    schema_name = tenant_schema_name(project.id)
    db.add(ProjectTenant(project_id=project.id, schema_name=schema_name))
    ensure_tenant_schema(db, schema_name)
    db.add(
        AgentEvent(
            run_id=run.id,
            seq=1,
            agent="user",
            type="message",
            phase="input",
            public_summary=body.description.strip(),
            payload={},
        )
    )
    db.commit()
    ensure_workspace(project.id)
    background.add_task(begin_run, run.id)
    return project_dict(db, project)


@app.get("/api/projects/archived")
def archived_projects(user: User = Depends(current_user), db: DbSession = Depends(get_db)):
    rows = db.scalars(
        select(Project).where(Project.owner_id == user.id, Project.archived_at.is_not(None)).order_by(Project.archived_at.desc())
    ).all()
    return [project_dict(db, row) for row in rows]


@app.post("/api/projects/{project_id}/archive")
def archive_project(project_id: str, user: User = Depends(current_user), db: DbSession = Depends(get_db)):
    project = project_for_user(db, project_id, user)
    if not project.archived_at:
        project.archived_at = datetime.now(timezone.utc)
        db.commit()
    return project_dict(db, project)


@app.post("/api/projects/{project_id}/restore")
def restore_project(project_id: str, user: User = Depends(current_user), db: DbSession = Depends(get_db)):
    project = project_for_user(db, project_id, user)
    project.archived_at = None
    db.commit()
    return project_dict(db, project)


@app.delete("/api/projects/{project_id}", status_code=204)
async def delete_project(project_id: str, user: User = Depends(current_user), db: DbSession = Depends(get_db)):
    project = project_for_user(db, project_id, user)
    if not project.archived_at:
        raise HTTPException(status_code=409, detail="请先归档项目，再永久删除")
    await deployment_provider.offline_project(project_id, remove_volume=True)
    preview_manager.stop(project_id)
    schema_name = tenant_schema_name(project_id)
    db.connection().exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')
    root = project_root(project_id)
    db.delete(project)
    db.commit()
    if root.exists():
        shutil.rmtree(root)


@app.get("/api/projects/{project_id}")
def get_project(project_id: str, user: User = Depends(current_user), db: DbSession = Depends(get_db)):
    return project_dict(db, project_for_user(db, project_id, user))


@app.patch("/api/projects/{project_id}/mode")
def update_mode(
    project_id: str,
    body: ModeUpdate,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
):
    project = project_for_user(db, project_id, user)
    project.mode = body.mode
    db.commit()
    return project_dict(db, project)


@app.post("/api/projects/{project_id}/messages", status_code=202)
def send_message(
    project_id: str,
    body: MessageCreate,
    background: BackgroundTasks,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
):
    project = project_for_user(db, project_id, user)
    active = db.scalar(
        select(Run).where(
            Run.project_id == project_id,
            Run.state.in_(["created", "planning", "awaiting_approval", "building", "testing"]),
        )
    )
    attachments = save_attachments(project.id, body.attachments)
    if active:
        last_seq = db.scalar(select(AgentEvent.seq).where(AgentEvent.run_id == active.id).order_by(AgentEvent.seq.desc()).limit(1)) or 0
        db.add(AgentEvent(run_id=active.id, seq=last_seq + 1, agent="user", type="message", phase="input", public_summary=body.content.strip() or f"发送了 {len(attachments)} 个附件", payload={"attachments": attachments, "steer": True}))
        db.commit()
        return run_dict(active)
    run = Run(
        id=str(uuid.uuid4()), project_id=project.id, goal=body.content.strip() or "分析附件并实现需求", mode=project.mode, state="created"
    )
    db.add(run)
    db.flush()
    db.add(
        AgentEvent(
            run_id=run.id,
            seq=1,
            agent="user",
            type="message",
            phase="input",
            public_summary=body.content.strip(),
            payload={"attachments": attachments},
        )
    )
    project.status = "created"
    db.commit()
    background.add_task(begin_run, run.id)
    return run_dict(run)


@app.get("/api/projects/{project_id}/attachments/{attachment_id}")
def download_attachment(project_id: str, attachment_id: str, user: User = Depends(event_user), db: DbSession = Depends(get_db)):
    project_for_user(db, project_id, user)
    if not re.fullmatch(r"[a-f0-9]{32}", attachment_id):
        raise HTTPException(status_code=404, detail="附件不存在")
    folder = ensure_workspace(project_id) / ".atoms" / "attachments"
    targets = list(folder.glob(f"{attachment_id}.*")) if folder.exists() else []
    if not targets:
        raise HTTPException(status_code=404, detail="附件不存在")
    return FileResponse(targets[0])


@app.get("/api/projects/{project_id}/runs")
def project_runs(project_id: str, user: User = Depends(current_user), db: DbSession = Depends(get_db)):
    project_for_user(db, project_id, user)
    rows = db.scalars(select(Run).where(Run.project_id == project_id).order_by(Run.created_at)).all()
    return [run_dict(row) for row in rows]


@app.get("/api/projects/{project_id}/events")
def project_events(project_id: str, user: User = Depends(current_user), db: DbSession = Depends(get_db)):
    project_for_user(db, project_id, user)
    rows = db.scalars(
        select(AgentEvent)
        .join(Run, AgentEvent.run_id == Run.id)
        .where(Run.project_id == project_id)
        .order_by(AgentEvent.id)
    ).all()
    return [event_dict(row) for row in rows]


@app.get("/api/runs/{run_id}/events/stream")
async def event_stream(run_id: str, user: User = Depends(event_user)):
    with SessionLocal() as db:
        run = db.get(Run, run_id)
        if not run:
            raise HTTPException(status_code=404, detail="运行不存在")
        project_for_user(db, run.project_id, user)

    async def generate():
        last_id = 0
        while True:
            with SessionLocal() as db:
                events = db.scalars(
                    select(AgentEvent).where(AgentEvent.run_id == run_id, AgentEvent.id > last_id).order_by(AgentEvent.id)
                ).all()
                run = db.get(Run, run_id)
                state = run.state if run else "failed"
            for event in events:
                last_id = event.id
                yield f"id: {event.id}\ndata: {json.dumps(event_dict(event), ensure_ascii=False)}\n\n"
            if state in {"preview_ready", "failed", "cancelled"}:
                yield f"event: done\ndata: {json.dumps({'state': state})}\n\n"
                break
            yield ": keepalive\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.post("/api/runs/{run_id}/approve", status_code=202)
def approve_run(
    run_id: str,
    background: BackgroundTasks,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
):
    run = db.get(Run, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="运行不存在")
    project_for_user(db, run.project_id, user)
    if run.state != "awaiting_approval":
        raise HTTPException(status_code=409, detail="当前状态无需批准")
    run.state = "building"
    db.commit()
    background.add_task(run_builder, run_id)
    return run_dict(run)


@app.post("/api/runs/{run_id}/revise", status_code=202)
def revise_run(
    run_id: str,
    body: MessageCreate,
    background: BackgroundTasks,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
):
    run = db.get(Run, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="运行不存在")
    project = project_for_user(db, run.project_id, user)
    if run.state != "awaiting_approval":
        raise HTTPException(status_code=409, detail="当前计划不可修改")
    last_seq = db.scalar(select(AgentEvent.seq).where(AgentEvent.run_id == run.id).order_by(AgentEvent.seq.desc()).limit(1)) or 0
    run.goal = f"{run.goal}\n\nUser requested plan changes: {body.content.strip()}"
    run.plan = None
    run.state = "planning"
    project.status = "planning"
    db.add(AgentEvent(run_id=run.id, seq=last_seq + 1, agent="user", type="feedback", phase="approval", public_summary=body.content.strip(), payload={"action": "revise_plan"}))
    db.commit()
    background.add_task(begin_run, run.id)
    return run_dict(run)


@app.get("/api/projects/{project_id}/files")
def files(project_id: str, user: User = Depends(current_user), db: DbSession = Depends(get_db)):
    project_for_user(db, project_id, user)
    return {"files": list_files(ensure_workspace(project_id))}


@app.get("/api/projects/{project_id}/file")
def file_content(
    project_id: str,
    path: str = Query(...),
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
):
    project_for_user(db, project_id, user)
    target = safe_path(project_root(project_id), path)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    stat = target.stat()
    return {"path": path, "content": target.read_text(encoding="utf-8"), "size": stat.st_size, "modified_at": stat.st_mtime_ns}


@app.post("/api/projects/{project_id}/file/format")
def format_file(
    project_id: str,
    body: FileUpdate,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
):
    project_for_user(db, project_id, user)
    safe_path(project_root(project_id), body.path)
    if not body.path.lower().endswith(".py"):
        return {"path": body.path, "content": body.content, "formatted": False}
    try:
        formatted = black.format_file_contents(body.content, fast=False, mode=black.Mode())
    except black.NothingChanged:
        formatted = body.content
    except (black.InvalidInput, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"Python 格式化失败: {exc}") from exc
    return {"path": body.path, "content": formatted, "formatted": True}


@app.put("/api/projects/{project_id}/file")
async def update_file(
    project_id: str,
    body: FileUpdate,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
):
    project = project_for_user(db, project_id, user)
    root = project_root(project_id)
    target = safe_path(root, body.path)
    allowed = {".py", ".html", ".css", ".js", ".jsx", ".ts", ".tsx", ".json", ".md", ".txt"}
    if target.suffix.lower() not in allowed:
        raise HTTPException(status_code=422, detail="该文件类型不允许在线编辑")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body.content, encoding="utf-8")
    checks = run_checks(root)
    restarted = False
    if checks["ok"] and project.status == "preview_ready":
        port = await preview_manager.start(project_id, root, project.preview_port)
        project.preview_port = port
        db.commit()
        restarted = True
    return {"path": body.path, "saved": True, "checks": checks, "preview_restarted": restarted, "modified_at": target.stat().st_mtime_ns}


@app.get("/api/projects/{project_id}/versions")
def versions(project_id: str, user: User = Depends(current_user), db: DbSession = Depends(get_db)):
    project_for_user(db, project_id, user)
    rows = db.scalars(select(Version).where(Version.project_id == project_id).order_by(Version.number.desc())).all()
    return [
        {"id": row.id, "number": row.number, "label": row.label, "build_status": row.build_status, "created_at": row.created_at.isoformat()}
        for row in rows
    ]


@app.get("/api/projects/{project_id}/export")
def export_project(project_id: str, user: User = Depends(current_user), db: DbSession = Depends(get_db)):
    project = project_for_user(db, project_id, user)
    root = project_root(project_id)
    export_dir = settings.workspace_root / "_exports" / project_id
    export_dir.mkdir(parents=True, exist_ok=True)
    archive = export_dir / f"{project.name.replace(' ', '-').lower()}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in root.rglob("*"):
            if path.is_file() and ".atoms" not in path.parts:
                bundle.write(path, path.relative_to(root))
    return FileResponse(archive, filename=f"{project.name}.zip", media_type="application/zip")


@app.post("/api/projects/{project_id}/publish", status_code=201)
async def publish_project(project_id: str, user: User = Depends(current_user), db: DbSession = Depends(get_db)):
    project = project_for_user(db, project_id, user)
    version = db.scalar(
        select(Version).where(Version.project_id == project_id).order_by(Version.number.desc()).limit(1)
    )
    if not version:
        raise HTTPException(status_code=409, detail="项目还没有可发布版本")
    deployment = Deployment(
        id=str(uuid.uuid4()), project_id=project.id, version_id=version.id,
        provider="atoms-cloud", status="building",
    )
    db.add(deployment)
    db.commit()
    try:
        result = await deployment_provider.publish(deployment.id, project, version)
        deployment.status = result.status
        deployment.url = result.url
        previous = db.scalars(select(Deployment).where(Deployment.project_id == project.id, Deployment.id != deployment.id)).all()
        for old in previous:
            db.delete(old)
        db.commit()
        response = deployment_dict(deployment)
        response.update({
            "app_container": (result.detail or {}).get("app_container"),
            "frontend_container": (result.detail or {}).get("frontend_container"),
            "backend_container": (result.detail or {}).get("backend_container"),
            "db_container": (result.detail or {}).get("db_container"),
            "metadata": (result.detail or {}).get("metadata", {}),
        })
        return response
    except CloudProviderError as exc:
        deployment.status = "failed"
        deployment.error = str(exc)
        db.commit()
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/projects/{project_id}/deployments")
def project_deployments(project_id: str, user: User = Depends(current_user), db: DbSession = Depends(get_db)):
    project_for_user(db, project_id, user)
    rows = db.scalars(select(Deployment).where(Deployment.project_id == project_id).order_by(Deployment.created_at.desc())).all()
    return [deployment_dict(row) for row in rows]


@app.get("/api/cloud/deployments")
async def cloud_deployments(user: User = Depends(current_user), db: DbSession = Depends(get_db)):
    project_ids = set(db.scalars(select(Project.id).where(Project.owner_id == user.id)).all())
    rows = db.scalars(select(Deployment).where(Deployment.project_id.in_(project_ids)).order_by(Deployment.created_at.desc())).all() if project_ids else []
    cloud_rows = {row["id"]: row for row in await deployment_provider.deployments() if row["project_id"] in project_ids}
    result = []
    for row in rows:
        remote = cloud_rows.get(row.id, {})
        if not remote:
            continue
        if remote:
            row.status = remote.get("status", row.status)
            row.url = remote.get("public_url", row.url)
            row.error = remote.get("error")
        item = deployment_dict(row)
        item.update({"project_name": remote.get("project_name"), "app_container": remote.get("app_container"), "frontend_container": remote.get("frontend_container"), "backend_container": remote.get("backend_container"), "db_container": remote.get("db_container"), "metadata": remote.get("metadata", {})})
        result.append(item)
    db.commit()
    return result


@app.delete("/api/cloud/deployments/{deployment_id}", status_code=204)
async def remove_cloud_deployment(deployment_id: str, remove_volume: bool = Query(default=False), user: User = Depends(current_user), db: DbSession = Depends(get_db)):
    row = db.get(Deployment, deployment_id)
    if not row:
        raise HTTPException(status_code=404, detail="发布记录不存在")
    project_for_user(db, row.project_id, user)
    await deployment_provider.delete(deployment_id, remove_volume)
    db.delete(row)
    db.commit()


@app.get("/api/cloud/containers")
async def cloud_containers(user: User = Depends(current_user), db: DbSession = Depends(get_db)):
    project_ids = set(db.scalars(select(Project.id).where(Project.owner_id == user.id)).all())
    return [item for item in await deployment_provider.containers() if item["project_id"] in project_ids]


@app.get("/api/cloud/containers/{container_id}/logs")
async def cloud_logs(container_id: str, tail: int = Query(default=300, ge=1, le=2000), user: User = Depends(current_user), db: DbSession = Depends(get_db)):
    await cloud_container_for_user(db, container_id, user)
    return await deployment_provider.logs(container_id, tail)


@app.post("/api/cloud/containers/{container_id}/action")
async def cloud_action(container_id: str, body: CloudContainerAction, user: User = Depends(current_user), db: DbSession = Depends(get_db)):
    await cloud_container_for_user(db, container_id, user)
    return await deployment_provider.action(container_id, body.action)


@app.post("/api/cloud/containers/{container_id}/exec")
async def cloud_exec(container_id: str, body: CloudExecRequest, user: User = Depends(current_user), db: DbSession = Depends(get_db)):
    await cloud_container_for_user(db, container_id, user)
    return await deployment_provider.exec(container_id, body.command)


@app.websocket("/api/cloud/containers/{container_id}/terminal")
async def cloud_terminal(websocket: WebSocket, container_id: str, token: str = Query(default="")):
    with SessionLocal() as db:
        try:
            user = user_from_token(db, token)
            await cloud_container_for_user(db, container_id, user)
        except HTTPException as exc:
            await websocket.close(code=4401 if exc.status_code == 401 else 4403)
            return
    await websocket.accept()
    cloud_ws = deployment_provider.base_url.replace("http://", "ws://").replace("https://", "wss://")
    uri = f"{cloud_ws}/terminal/{container_id}?key={settings.cloud_api_key}"
    try:
        async with websockets.connect(uri, max_size=None) as upstream:
            async def browser_to_cloud():
                while True:
                    message = await websocket.receive()
                    if message.get("type") == "websocket.disconnect":
                        break
                    await upstream.send(message.get("bytes") or message.get("text") or "")

            async def cloud_to_browser():
                async for message in upstream:
                    if isinstance(message, bytes):
                        await websocket.send_bytes(message)
                    else:
                        await websocket.send_text(message)

            tasks = [asyncio.create_task(browser_to_cloud()), asyncio.create_task(cloud_to_browser())]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in tasks:
                task.cancel()
    except (WebSocketDisconnect, websockets.ConnectionClosed):
        pass
    except Exception as exc:
        try:
            await websocket.send_text(f"\r\n[terminal disconnected: {exc}]\r\n")
        except Exception:
            pass


@app.delete("/api/cloud/projects/{project_id}/stack", status_code=204)
async def offline_cloud_project(project_id: str, remove_volume: bool = Query(default=False), user: User = Depends(current_user), db: DbSession = Depends(get_db)):
    project_for_user(db, project_id, user)
    await deployment_provider.offline_project(project_id, remove_volume)
    rows = db.scalars(select(Deployment).where(Deployment.project_id == project_id)).all()
    for row in rows:
        db.delete(row)
    db.commit()


@app.api_route("/preview/{project_id}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def preview_proxy(project_id: str, path: str, request: Request):
    with SessionLocal() as db:
        project = db.get(Project, project_id)
        if not project:
            raise HTTPException(status_code=404, detail="预览不存在")
        preferred = project.preview_port
    port = preview_manager.port_for(project_id)
    if not port:
        port = await preview_manager.start(project_id, project_root(project_id), preferred)
        with SessionLocal() as db:
            project = db.get(Project, project_id)
            if project:
                project.preview_port = port
                db.commit()
    target = f"http://127.0.0.1:{port}/{path}"
    headers = {key: value for key, value in request.headers.items() if key.lower() not in {"host", "content-length"}}
    body = await request.body()
    async with httpx.AsyncClient(follow_redirects=True) as client:
        response = await client.request(request.method, target, params=request.query_params, headers=headers, content=body, timeout=30)
    excluded = {"content-encoding", "transfer-encoding", "connection", "content-length"}
    response_headers = {key: value for key, value in response.headers.items() if key.lower() not in excluded}
    return Response(content=response.content, status_code=response.status_code, headers=response_headers)


@app.get("/preview/{project_id}")
async def preview_root(project_id: str, request: Request):
    return await preview_proxy(project_id, "", request)

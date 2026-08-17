import asyncio
import hashlib
import hmac
import secrets
import shutil
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import docker
import httpx
import psycopg2
from docker.errors import APIError, BuildError, ImageNotFound, NotFound
from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from psycopg2.extras import Json, RealDictCursor
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class CloudSettings(BaseSettings):
    database_url: str = "postgresql://cloud:cloud-local-password@cloud-db:5432/atoms_cloud"
    api_key: str = "atoms-cloud-local-key"
    docker_network: str = "atoms-cloud-net"
    public_host: str = "localhost"
    workspace_root: Path = Path("/workspaces")
    data_root: Path = Path("/cloud/data")
    build_timeout_seconds: int = 600
    app_memory_limit: str = "768m"
    database_memory_limit: str = "512m"
    app_cpu_limit: float = 1.5
    database_cpu_limit: float = 1.0
    container_pids_limit: int = 256
    published_port_start: int = 20_000
    published_port_end: int = 45_000

    model_config = SettingsConfigDict(env_prefix="CLOUD_", extra="ignore")


settings = CloudSettings()
settings.data_root.mkdir(parents=True, exist_ok=True)
docker_client = docker.from_env(timeout=settings.build_timeout_seconds)


class PublishRequest(BaseModel):
    deployment_id: str = Field(pattern=r"^[a-f0-9-]{36}$")
    project_id: str = Field(pattern=r"^[a-f0-9-]{36}$")
    project_name: str = Field(min_length=1, max_length=160)
    version_number: int = Field(ge=1)
    snapshot_path: str = Field(min_length=1, max_length=500)


class ContainerAction(BaseModel):
    action: Literal["start", "stop", "restart", "remove"]


class ExecRequest(BaseModel):
    command: str = Field(min_length=1, max_length=4000)
    timeout_seconds: int = Field(default=30, ge=1, le=120)


def require_cloud_key(x_cloud_key: str = Header(default="")) -> None:
    if not secrets.compare_digest(x_cloud_key, settings.api_key):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid cloud API key")


def db_connection():
    return psycopg2.connect(settings.database_url, cursor_factory=RealDictCursor)


def init_database() -> None:
    for attempt in range(30):
        try:
            with db_connection() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """CREATE TABLE IF NOT EXISTS cloud_deployments (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        project_name TEXT NOT NULL,
                        version_number INTEGER NOT NULL,
                        status TEXT NOT NULL,
                        public_url TEXT,
                        app_container TEXT,
                        frontend_container TEXT,
                        backend_container TEXT,
                        db_container TEXT,
                        image_tag TEXT,
                        volume_name TEXT,
                        error TEXT,
                        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )"""
                )
                cursor.execute("ALTER TABLE cloud_deployments ADD COLUMN IF NOT EXISTS frontend_container TEXT")
                cursor.execute("ALTER TABLE cloud_deployments ADD COLUMN IF NOT EXISTS backend_container TEXT")
                cursor.execute("CREATE INDEX IF NOT EXISTS cloud_deployments_project_idx ON cloud_deployments(project_id, created_at DESC)")
            return
        except psycopg2.OperationalError:
            if attempt == 29:
                raise
            time.sleep(1)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_database()
    ensure_network()
    yield


app = FastAPI(title="Atoms Cloud", version="0.1.0", lifespan=lifespan)


def ensure_network():
    try:
        return docker_client.networks.get(settings.docker_network)
    except NotFound:
        return docker_client.networks.create(settings.docker_network, driver="bridge", attachable=True)


def project_database_password(project_id: str) -> str:
    return hmac.new(settings.api_key.encode(), f"project-db:{project_id}".encode(), hashlib.sha256).hexdigest()[:48]


def deployment_row(deployment_id: str) -> dict:
    with db_connection() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT * FROM cloud_deployments WHERE id = %s", (deployment_id,))
        row = cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Deployment not found")
    return dict(row)


def update_deployment(deployment_id: str, **values) -> None:
    if not values:
        return
    allowed = {"status", "public_url", "app_container", "frontend_container", "backend_container", "db_container", "image_tag", "volume_name", "error", "metadata"}
    clean = {key: value for key, value in values.items() if key in allowed}
    assignments = ", ".join(f"{key} = %s" for key in clean)
    params = [Json(value) if key == "metadata" else value for key, value in clean.items()]
    with db_connection() as connection, connection.cursor() as cursor:
        cursor.execute(f"UPDATE cloud_deployments SET {assignments}, updated_at = CURRENT_TIMESTAMP WHERE id = %s", (*params, deployment_id))


def safe_snapshot(relative: str) -> Path:
    if relative.startswith(("/", "\\")) or ".." in Path(relative).parts:
        raise HTTPException(status_code=422, detail="Invalid snapshot path")
    snapshot = (settings.workspace_root / relative).resolve()
    root = settings.workspace_root.resolve()
    if root not in snapshot.parents or snapshot.suffix.lower() != ".zip" or not snapshot.is_file():
        raise HTTPException(status_code=404, detail="Version snapshot not found")
    return snapshot


def extract_snapshot(snapshot: Path, target: Path) -> None:
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    with zipfile.ZipFile(snapshot) as archive:
        for info in archive.infolist():
            destination = (target / info.filename).resolve()
            if target.resolve() not in destination.parents and destination != target.resolve():
                raise HTTPException(status_code=422, detail="Unsafe file in version snapshot")
        archive.extractall(target)


GENERATED_BACKEND_DOCKERFILE = """FROM python:3.11-slim
RUN useradd --create-home --uid 10001 app
WORKDIR /app
COPY requirements.atoms-cloud.txt /tmp/requirements.atoms-cloud.txt
RUN pip install --no-cache-dir -r /tmp/requirements.atoms-cloud.txt
COPY . .
RUN chown -R app:app /app
USER app
EXPOSE 8000
HEALTHCHECK --interval=5s --timeout=3s --retries=20 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health')"
CMD ["python", "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
"""

GENERATED_FRONTEND_DOCKERFILE = """FROM nginx:1.27-alpine
COPY frontend /usr/share/nginx/html
COPY nginx.atoms-cloud.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
HEALTHCHECK --interval=5s --timeout=3s --retries=20 CMD wget -q -O /dev/null http://127.0.0.1/
"""


def write_build_files(build_dir: Path, backend_name: str) -> None:
    (build_dir / "Dockerfile.backend.atoms-cloud").write_text(GENERATED_BACKEND_DOCKERFILE, encoding="utf-8")
    (build_dir / "Dockerfile.frontend.atoms-cloud").write_text(GENERATED_FRONTEND_DOCKERFILE, encoding="utf-8")
    (build_dir / "nginx.atoms-cloud.conf").write_text(f"""server {{
  listen 80;
  root /usr/share/nginx/html;
  index index.html;
  location /api/ {{ proxy_pass http://{backend_name}:8000/api/; proxy_set_header Host $host; }}
  location / {{ try_files $uri $uri/ /index.html; }}
}}\n""", encoding="utf-8")
    (build_dir / ".dockerignore").write_text(".atoms\n__pycache__\n*.pyc\n", encoding="utf-8")
    requirements = "fastapi==0.116.1\nuvicorn[standard]==0.35.0\npsycopg2-binary==2.9.10\npydantic==2.11.7\n"
    project_requirements = build_dir / "requirements.txt"
    if project_requirements.is_file():
        content = project_requirements.read_text(encoding="utf-8")
        if len(content) > 100_000:
            raise HTTPException(status_code=422, detail="Project requirements.txt is too large")
        requirements = f"{content.rstrip()}\n{requirements}"
    (build_dir / "requirements.atoms-cloud.txt").write_text(requirements, encoding="utf-8")


def managed_container(container_id: str):
    try:
        container = docker_client.containers.get(container_id)
    except NotFound as exc:
        raise HTTPException(status_code=404, detail="Container not found") from exc
    if container.labels.get("atoms.cloud.managed") != "true":
        raise HTTPException(status_code=403, detail="Container is not managed by Atoms Cloud")
    return container


def reconcile_deployment_status(deployment_id: str) -> str:
    row = deployment_row(deployment_id)
    states: dict[str, str] = {}
    for role, container_id in (("frontend", row.get("frontend_container") or row.get("app_container")), ("backend", row.get("backend_container")), ("postgres", row.get("db_container"))):
        if not container_id:
            continue
        try:
            container = managed_container(container_id)
            container.reload()
            states[role] = container.status
        except HTTPException:
            states[role] = "missing"
    if states.get("frontend") != "running" or states.get("backend") != "running":
        next_status = "stopped"
    elif states.get("postgres") != "running":
        next_status = "degraded"
    else:
        next_status = "running"
    update_deployment(deployment_id, status=next_status)
    return next_status


def container_dict(container) -> dict:
    container.reload()
    state = container.attrs.get("State", {})
    ports = container.attrs.get("NetworkSettings", {}).get("Ports", {}) or {}
    return {
        "id": container.id,
        "short_id": container.short_id,
        "name": container.name,
        "image": container.image.tags[0] if container.image.tags else container.image.short_id,
        "status": container.status,
        "health": (state.get("Health") or {}).get("Status"),
        "role": container.labels.get("atoms.cloud.role", "service"),
        "deployment_id": container.labels.get("atoms.cloud.deployment_id"),
        "project_id": container.labels.get("atoms.cloud.project_id"),
        "ports": ports,
        "created": container.attrs.get("Created"),
    }


def wait_for_postgres(container, timeout: int = 60) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        container.reload()
        if container.status == "exited":
            raise RuntimeError(container.logs(tail=80).decode(errors="replace"))
        result = container.exec_run(["pg_isready", "-U", "app", "-d", "app"])
        if result.exit_code == 0:
            return
        time.sleep(1)
    raise RuntimeError("PostgreSQL container did not become ready")


def wait_for_container_health(container, timeout: int = 60) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        container.reload()
        state = container.attrs.get("State", {})
        if container.status == "exited":
            raise RuntimeError(container.logs(tail=80).decode(errors="replace"))
        health = (state.get("Health") or {}).get("Status")
        if health == "healthy" or (health is None and container.status == "running"):
            return
        if health == "unhealthy":
            raise RuntimeError(container.logs(tail=80).decode(errors="replace"))
        time.sleep(1)
    raise RuntimeError("Container did not become healthy")


def host_port(container, container_port: str = "80/tcp") -> int:
    container.reload()
    binding = container.attrs["NetworkSettings"]["Ports"].get(container_port)
    if not binding:
        raise RuntimeError("Application container has no published port")
    return int(binding[0]["HostPort"])


def published_port_candidates(deployment_id: str):
    span = settings.published_port_end - settings.published_port_start
    if span < 100:
        raise RuntimeError("Cloud published port range is too small")
    initial = int(hashlib.sha256(deployment_id.encode()).hexdigest()[:8], 16) % span
    for offset in range(min(span, 500)):
        yield settings.published_port_start + ((initial + offset) % span)


async def wait_for_application(container_name: str, timeout: int = 90) -> None:
    deadline = time.time() + timeout
    async with httpx.AsyncClient() as client:
        while time.time() < deadline:
            try:
                response = await client.get(f"http://{container_name}:8000/api/health", timeout=2)
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(1)
    raise RuntimeError("Application container did not become healthy")


@app.get("/health")
def health():
    docker_client.ping()
    return {"status": "ok", "docker": "connected", "network": settings.docker_network}


@app.post("/deployments", status_code=201, dependencies=[Depends(require_cloud_key)])
async def publish(body: PublishRequest):
    snapshot = safe_snapshot(body.snapshot_path)
    deployment_id = body.deployment_id
    short = deployment_id.replace("-", "")[:12]
    project_short = body.project_id.replace("-", "")[:12]
    frontend_name = f"atoms-frontend-{project_short}"
    backend_name = f"atoms-backend-{project_short}"
    db_name = f"atoms-db-{project_short}"
    frontend_image = f"atoms-generated-frontend:{short}"
    backend_image = f"atoms-generated-backend:{short}"
    image_tag = f"{frontend_image},{backend_image}"
    volume_name = f"atoms-cloud-pg-{project_short}"
    build_dir = settings.data_root / "builds" / deployment_id
    labels = {
        "atoms.cloud.managed": "true",
        "atoms.cloud.deployment_id": deployment_id,
        "atoms.cloud.project_id": body.project_id,
    }
    with db_connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """INSERT INTO cloud_deployments(id, project_id, project_name, version_number, status, metadata)
               VALUES (%s, %s, %s, %s, 'building', %s)
               ON CONFLICT (id) DO UPDATE SET status='building', error=NULL, updated_at=CURRENT_TIMESTAMP""",
            (deployment_id, body.project_id, body.project_name, body.version_number, Json({"snapshot": body.snapshot_path})),
        )
    try:
        extract_snapshot(snapshot, build_dir)
        write_build_files(build_dir, backend_name)
        docker_client.images.build(path=str(build_dir), dockerfile="Dockerfile.backend.atoms-cloud", tag=backend_image, rm=True, labels=labels)
        docker_client.images.build(path=str(build_dir), dockerfile="Dockerfile.frontend.atoms-cloud", tag=frontend_image, rm=True, labels=labels)
        update_deployment(deployment_id, status="provisioning", image_tag=image_tag, volume_name=volume_name)
        network = ensure_network()
        # A project owns one live stack. Remove the previous frontend/backend before replacing it.
        for old in docker_client.containers.list(all=True, filters={"label": ["atoms.cloud.managed=true", f"atoms.cloud.project_id={body.project_id}"]}):
            if old.labels.get("atoms.cloud.role") in {"application", "frontend", "backend"}:
                old.remove(force=True)
        with db_connection() as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM cloud_deployments WHERE project_id=%s AND id<>%s", (body.project_id, deployment_id))
        try:
            db_container = docker_client.containers.get(db_name)
            if db_container.labels.get("atoms.cloud.managed") != "true" or db_container.labels.get("atoms.cloud.project_id") != body.project_id:
                raise RuntimeError(f"Container name {db_name} is already used outside this project")
            db_container.reload()
            password_entry = next((item for item in db_container.attrs.get("Config", {}).get("Env", []) if item.startswith("POSTGRES_PASSWORD=")), None)
            if not password_entry:
                raise RuntimeError("Project PostgreSQL container has no managed password")
            password = password_entry.split("=", 1)[1]
            if db_container.status != "running":
                db_container.start()
        except NotFound:
            password = project_database_password(body.project_id)
            try:
                volume = docker_client.volumes.get(volume_name)
            except NotFound:
                volume = docker_client.volumes.create(name=volume_name, labels={
                    "atoms.cloud.managed": "true",
                    "atoms.cloud.project_id": body.project_id,
                    "atoms.cloud.role": "postgres-data",
                })
            db_container = docker_client.containers.run(
                "postgres:16-alpine",
                name=db_name,
                detach=True,
                environment={"POSTGRES_DB": "app", "POSTGRES_USER": "app", "POSTGRES_PASSWORD": password},
                volumes={volume.name: {"bind": "/var/lib/postgresql/data", "mode": "rw"}},
                network=network.name,
                labels={
                    "atoms.cloud.managed": "true",
                    "atoms.cloud.project_id": body.project_id,
                    "atoms.cloud.role": "postgres",
                },
                restart_policy={"Name": "unless-stopped"},
                mem_limit=settings.database_memory_limit,
                nano_cpus=int(settings.database_cpu_limit * 1_000_000_000),
                pids_limit=settings.container_pids_limit,
            )
        update_deployment(deployment_id, db_container=db_container.id)
        wait_for_postgres(db_container)
        schema = f"tenant_{body.project_id.replace('-', '')}"
        backend_container = docker_client.containers.run(
            backend_image,
            name=backend_name,
            detach=True,
            environment={
                "PROJECT_DATABASE_URL": f"postgresql://app:{password}@{db_name}:5432/app",
                "PROJECT_TENANT_SCHEMA": schema,
            },
            network=network.name,
            labels={**labels, "atoms.cloud.role": "backend"},
            restart_policy={"Name": "unless-stopped"},
            mem_limit=settings.app_memory_limit,
            nano_cpus=int(settings.app_cpu_limit * 1_000_000_000),
            pids_limit=settings.container_pids_limit,
        )
        update_deployment(deployment_id, backend_container=backend_container.id)
        wait_for_container_health(backend_container, timeout=90)
        frontend_container = None
        for published_port in published_port_candidates(deployment_id):
            try:
                frontend_container = docker_client.containers.run(
                    frontend_image,
                    name=frontend_name,
                    detach=True,
                    network=network.name,
                    ports={"80/tcp": published_port},
                    labels={**labels, "atoms.cloud.role": "frontend"},
                    restart_policy={"Name": "unless-stopped"},
                    mem_limit="256m",
                    nano_cpus=int(settings.app_cpu_limit * 1_000_000_000),
                    pids_limit=settings.container_pids_limit,
                )
                break
            except APIError as exc:
                try:
                    docker_client.containers.get(frontend_name).remove(force=True)
                except NotFound:
                    pass
                if "port is already allocated" not in str(exc).lower() and "address already in use" not in str(exc).lower():
                    raise
        if frontend_container is None:
            raise RuntimeError("No available published port in the configured Cloud range")
        update_deployment(deployment_id, app_container=frontend_container.id, frontend_container=frontend_container.id)
        wait_for_container_health(frontend_container, timeout=60)
        port = host_port(frontend_container)
        public_url = f"http://{settings.public_host}:{port}"
        update_deployment(
            deployment_id,
            status="running",
            public_url=public_url,
            app_container=frontend_container.id,
            frontend_container=frontend_container.id,
            backend_container=backend_container.id,
            db_container=db_container.id,
            error=None,
            metadata={"snapshot": body.snapshot_path, "host_port": port, "network": network.name, "services": ["frontend", "backend", "postgres"]},
        )
        return deployment_row(deployment_id)
    except HTTPException as exc:
        update_deployment(deployment_id, status="failed", error=str(exc.detail)[-4000:])
        raise
    except Exception as exc:
        update_deployment(deployment_id, status="failed", error=str(exc)[-4000:])
        raise HTTPException(status_code=500, detail=f"Cloud publish failed: {str(exc)[-1200:]}") from exc


@app.get("/deployments", dependencies=[Depends(require_cloud_key)])
def deployments(project_id: str | None = Query(default=None)):
    with db_connection() as connection, connection.cursor() as cursor:
        if project_id:
            cursor.execute("SELECT * FROM cloud_deployments WHERE project_id=%s ORDER BY created_at DESC", (project_id,))
        else:
            cursor.execute("SELECT * FROM cloud_deployments ORDER BY created_at DESC")
        return [dict(row) for row in cursor.fetchall()]


@app.get("/deployments/{deployment_id}", dependencies=[Depends(require_cloud_key)])
def get_deployment(deployment_id: str):
    return deployment_row(deployment_id)


@app.delete("/deployments/{deployment_id}", status_code=204, dependencies=[Depends(require_cloud_key)])
def delete_deployment(deployment_id: str, remove_volume: bool = Query(default=False)):
    row = deployment_row(deployment_id)
    container_ids = [row.get("frontend_container") or row.get("app_container"), row.get("backend_container")]
    if remove_volume:
        container_ids.append(row.get("db_container"))
    for container_id in container_ids:
        if container_id:
            try:
                managed_container(container_id).remove(force=True)
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
    for image_tag in (row.get("image_tag") or "").split(","):
        if image_tag:
            try:
                docker_client.images.remove(image_tag, force=True)
            except (ImageNotFound, APIError):
                pass
    if remove_volume and row.get("volume_name"):
        try:
            docker_client.volumes.get(row["volume_name"]).remove(force=True)
        except (NotFound, APIError):
            pass
    with db_connection() as connection, connection.cursor() as cursor:
        cursor.execute("DELETE FROM cloud_deployments WHERE id=%s", (deployment_id,))


@app.delete("/projects/{project_id}/stack", status_code=204, dependencies=[Depends(require_cloud_key)])
def offline_project_stack(project_id: str, remove_volume: bool = Query(default=False)):
    filters = {"label": ["atoms.cloud.managed=true", f"atoms.cloud.project_id={project_id}"]}
    for container in docker_client.containers.list(all=True, filters=filters):
        container.remove(force=True)
    with db_connection() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT image_tag, volume_name FROM cloud_deployments WHERE project_id=%s", (project_id,))
        rows = list(cursor.fetchall())
        cursor.execute("DELETE FROM cloud_deployments WHERE project_id=%s", (project_id,))
    for row in rows:
        for image_tag in (row.get("image_tag") or "").split(","):
            if image_tag:
                try:
                    docker_client.images.remove(image_tag, force=True)
                except (ImageNotFound, APIError):
                    pass
        if remove_volume and row.get("volume_name"):
            try:
                docker_client.volumes.get(row["volume_name"]).remove(force=True)
            except (NotFound, APIError):
                pass


@app.get("/containers", dependencies=[Depends(require_cloud_key)])
def containers(deployment_id: str | None = Query(default=None)):
    filters = {"label": ["atoms.cloud.managed=true"]}
    if deployment_id:
        row = deployment_row(deployment_id)
        expected_ids = {item for item in (row.get("frontend_container") or row.get("app_container"), row.get("backend_container"), row.get("db_container")) if item}
        return [container_dict(container) for container in docker_client.containers.list(all=True, filters=filters) if container.id in expected_ids]
    return [container_dict(container) for container in docker_client.containers.list(all=True, filters=filters)]


@app.get("/containers/{container_id}/logs", dependencies=[Depends(require_cloud_key)])
def container_logs(container_id: str, tail: int = Query(default=300, ge=1, le=2000)):
    container = managed_container(container_id)
    return {"container": container_dict(container), "logs": container.logs(stdout=True, stderr=True, tail=tail, timestamps=True).decode(errors="replace")}


@app.post("/containers/{container_id}/action", dependencies=[Depends(require_cloud_key)])
def act_on_container(container_id: str, body: ContainerAction):
    container = managed_container(container_id)
    deployment_id = container.labels.get("atoms.cloud.deployment_id")
    role = container.labels.get("atoms.cloud.role")
    if body.action == "start":
        container.start()
    elif body.action == "stop":
        container.stop(timeout=10)
    elif body.action == "restart":
        container.restart(timeout=10)
    else:
        container.remove(force=True)
    if body.action == "remove":
        return {"id": container_id, "status": "removed"}
    if role in {"application", "frontend", "backend"} and body.action in {"start", "restart"}:
        wait_for_container_health(container)
    if role == "postgres" and body.action in {"start", "restart"}:
        wait_for_postgres(container)
    result = container_dict(container)
    if deployment_id and role in {"application", "frontend", "backend", "postgres"}:
        reconcile_deployment_status(deployment_id)
    if deployment_id and role in {"application", "frontend"} and body.action in {"start", "restart"}:
        port = host_port(container)
        row = deployment_row(deployment_id)
        metadata = dict(row.get("metadata") or {})
        metadata["host_port"] = port
        public_url = f"http://{settings.public_host}:{port}"
        update_deployment(deployment_id, public_url=public_url, metadata=metadata)
        result["public_url"] = public_url
    return result


@app.post("/containers/{container_id}/exec", dependencies=[Depends(require_cloud_key)])
def exec_in_container(container_id: str, body: ExecRequest):
    container = managed_container(container_id)
    result = container.exec_run(["/bin/sh", "-lc", body.command], demux=True, workdir="/")
    stdout, stderr = result.output if isinstance(result.output, tuple) else (result.output, b"")
    return {
        "exit_code": result.exit_code,
        "stdout": (stdout or b"").decode(errors="replace")[-50_000:],
        "stderr": (stderr or b"").decode(errors="replace")[-50_000:],
    }


@app.websocket("/terminal/{container_id}")
async def terminal(container_id: str, websocket: WebSocket, key: str = Query(default="")):
    if not secrets.compare_digest(key, settings.api_key):
        await websocket.close(code=4401)
        return
    try:
        container = managed_container(container_id)
    except HTTPException as exc:
        await websocket.close(code=4404 if exc.status_code == 404 else 4403)
        return
    container.reload()
    if container.status != "running":
        await websocket.close(code=4409, reason="Container is not running")
        return
    await websocket.accept()
    exec_id = docker_client.api.exec_create(container.id, ["/bin/sh"], stdin=True, stdout=True, stderr=True, tty=True, workdir="/")["Id"]
    stream = docker_client.api.exec_start(exec_id, detach=False, tty=True, socket=True)
    raw_socket = getattr(stream, "_sock", stream)

    async def docker_to_client():
        while True:
            chunk = await asyncio.to_thread(raw_socket.recv, 8192)
            if not chunk:
                break
            await websocket.send_bytes(chunk)

    async def client_to_docker():
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            data = message.get("bytes") or (message.get("text") or "").encode()
            if data:
                await asyncio.to_thread(raw_socket.sendall, data)

    tasks = [asyncio.create_task(docker_to_client()), asyncio.create_task(client_to_docker())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except WebSocketDisconnect:
        pass
    finally:
        for task in tasks:
            task.cancel()
        try:
            raw_socket.close()
        except Exception:
            pass

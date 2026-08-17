import asyncio
import difflib
import hashlib
import json
import re
import uuid
import base64
import mimetypes
from io import BytesIO
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from .config import settings
from .db import SessionLocal
from .deployments import deployment_provider
from .llm import LLMError, RightAPIClient, output_text, parse_json_text
from .models import AgentEvent, Deployment, Project, Run, Version
from .preview import preview_manager
from .workspace import ensure_workspace, list_files, project_manifest, run_checks, safe_path, save_snapshot


def attachment_text(path: Path, mime: str) -> str:
    raw = path.read_bytes()
    if mime.startswith("text/") or mime == "application/json":
        return raw.decode("utf-8", errors="replace")[:80_000]
    if mime == "application/pdf":
        from pypdf import PdfReader
        return "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(raw)).pages)[:80_000]
    if mime.endswith("wordprocessingml.document"):
        from docx import Document
        return "\n".join(p.text for p in Document(BytesIO(raw)).paragraphs)[:80_000]
    return ""


def user_event_content(root: Path, event: AgentEvent) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = []
    if event.public_summary:
        content.append({"type": "input_text", "text": event.public_summary})
    for item in (event.payload or {}).get("attachments", []):
        target = safe_path(root, item["path"])
        mime = item.get("mime") or mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if mime.startswith("image/"):
            encoded = base64.b64encode(target.read_bytes()).decode()
            content.append({"type": "input_image", "image_url": f"data:{mime};base64,{encoded}"})
            content.append({"type": "input_text", "text": f"Attached image: {item['name']}"})
        else:
            try:
                extracted = attachment_text(target, mime)
            except Exception as exc:
                extracted = f"[Could not extract document: {exc}]"
            content.append({"type": "input_text", "text": f"Attached document {item['name']}:\n{extracted}"})
    return content or [{"type": "input_text", "text": "Continue with the latest request."}]


def pending_user_inputs(run_id: str, root: Path, after_id: int = 0) -> tuple[list[dict[str, Any]], int]:
    with SessionLocal() as db:
        rows = db.scalars(select(AgentEvent).where(AgentEvent.run_id == run_id, AgentEvent.agent == "user", AgentEvent.id > after_id).order_by(AgentEvent.id)).all()
    return ([{"role": "user", "content": user_event_content(root, row)} for row in rows], rows[-1].id if rows else after_id)


TOOLS = [
    {
        "type": "function",
        "name": "list_files",
        "description": "List every file in the generated website workspace.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        "strict": True,
    },
    {
        "type": "function",
        "name": "search_files",
        "description": "Search text across workspace files before deciding what to edit. Returns paths, line numbers and matching lines.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "path": {"type": "string"}},
            "required": ["query", "path"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "read_file",
        "description": "Read a UTF-8 text file before editing it.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "write_file",
        "description": "Create or completely replace a UTF-8 file in the workspace.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "replace_text",
        "description": "Replace one exact text occurrence in an existing file. Read it first.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            "required": ["path", "old_text", "new_text"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "run_checks",
        "description": "Validate the complete frontend/backend website. Always call before finish.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        "strict": True,
    },
    {
        "type": "function",
        "name": "publish_project",
        "description": "Queue the verified project for deployment to Atoms Cloud. Use only when the user explicitly asks to publish, deploy, or run it in Docker.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        "strict": True,
    },
    {
        "type": "function",
        "name": "finish",
        "description": "Finish only after run_checks reports ok=true.",
        "parameters": {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
            "additionalProperties": False,
        },
        "strict": True,
    },
]


AGENT_INSTRUCTIONS = """You are Alex, an autonomous full-stack coding agent. Use a ReAct loop like a strong terminal coding assistant: observe the current repository, choose the smallest useful next action, inspect the result, then adapt. Do not follow a fixed file order and never repeat an edit or check without a new observation that justifies it.

Operating rules:
- Start by listing or searching only what is relevant to the request. Read the files you will change.
- Make one coherent implementation slice at a time. Parallel independent reads are fine; do not batch speculative writes.
- After each tool result, treat it as a new observation and revise the next action.
- If a tool returns an error or no-op diff, diagnose it instead of retrying the same call.
- Run checks after a meaningful set of changes, not after every file. Use failures to target repairs.
- Finish with a concrete outcome summary: features delivered, important files/behaviors, and verification performed.

Repository contract:
- backend/main.py is a FastAPI app and must keep GET /api/health.
- frontend/index.html is the browser entry point. Organize non-trivial code into domain-appropriate nested modules such as frontend/src/components, frontend/src/pages, frontend/src/services, backend/api, backend/models and backend/services. The structure must follow the product, not a fixed template.
- Treat existing starter files as disposable scaffolding, not a product template. Do not put a whole application into backend/main.py or one frontend/app.js; keep entry points small and extract cohesive domain modules.
- The FastAPI app serves frontend files and implements real JSON API endpoints, including useful create/update/delete operations.
- Every project is a PostgreSQL tenant. Preserve psycopg2, PROJECT_DATABASE_URL, PROJECT_TENANT_SCHEMA, safe schema validation, CREATE SCHEMA, and tenant search_path plumbing from the starter.
- Never use SQLite, in-memory-only state, localStorage as the source of truth, or hard-coded arrays as the final data layer.
- Frontend controls must call real GET and write APIs and refresh from the backend after mutations.
- Frontend API requests must use relative URLs like ./api/items so preview routing works.
- Deliver a coherent, polished, responsive product, not a generic landing-page placeholder.
- Add realistic seeded demo data in backend code when useful.
- Do not require external accounts, CDNs, package installs or user-provided secrets; PostgreSQL is injected by the platform.
- Do not expose secrets. Do not create binary files.
- Read files before replacing targeted text. You may fully rewrite a file when a coherent redesign is needed.
- Always run checks. If checks fail, repair and rerun. Call finish only after checks are green.
- If the user explicitly asks to publish or deploy, call publish_project; it will deploy the immutable version after checks pass.

Do not explain your private reasoning. Tool calls and a concise final summary are sufficient."""


def change_summary(before: str, after: str) -> dict[str, Any]:
    diff = list(difflib.ndiff(before.splitlines(), after.splitlines()))
    return {
        "added": sum(1 for line in diff if line.startswith("+ ")),
        "removed": sum(1 for line in diff if line.startswith("- ")),
        "before_hash": hashlib.sha256(before.encode()).hexdigest()[:12],
        "after_hash": hashlib.sha256(after.encode()).hexdigest()[:12],
    }


def add_event(run_id: str, agent: str, event_type: str, summary: str, phase: str = "", payload: dict | None = None) -> None:
    with SessionLocal() as db:
        last = db.scalar(select(func.max(AgentEvent.seq)).where(AgentEvent.run_id == run_id)) or 0
        db.add(
            AgentEvent(
                run_id=run_id,
                seq=last + 1,
                agent=agent,
                type=event_type,
                phase=phase,
                public_summary=summary,
                payload=payload or {},
            )
        )
        db.commit()


def set_run_state(run_id: str, state: str, error: str | None = None) -> None:
    with SessionLocal() as db:
        run = db.get(Run, run_id)
        if run:
            run.state = state
            run.error = error
            project = db.get(Project, run.project_id)
            if project:
                project.status = state
            db.commit()


async def create_plan(run_id: str) -> None:
    with SessionLocal() as db:
        run = db.get(Run, run_id)
        project = db.get(Project, run.project_id) if run else None
        if not run or not project:
            return
        goal = run.goal
    set_run_state(run_id, "planning")
    add_event(run_id, "mike", "stage", "正在理解目标、识别关键决策并安排专业 Agent", "discovery", {"stage": "discovery", "status": "in_progress"})
    add_event(run_id, "emma", "stage", "正在定义用户、MVP 范围和可验收标准", "product", {"stage": "product", "status": "in_progress"})
    client = RightAPIClient()
    prompt = f"""Project request: {goal}
Return only JSON with keys: summary, target_users (array), features (array of strings), pages (array of strings), acceptance_criteria (array of strings), architecture (object with frontend, backend, database), data_model (array of strings), decisions_required (array of strings), implementation_steps (array of objects with title, owner and deliverable). Keep it implementable as a FastAPI plus HTML/CSS/JavaScript app with PostgreSQL tenant persistence."""
    try:
        response = await client.response(
            instructions="You are Emma, a concise product manager. Produce practical structured product specifications.",
            input=prompt,
            max_output_tokens=1800,
        )
        plan = parse_json_text(output_text(response))
    except Exception:
        plan = {
            "summary": goal,
            "target_users": ["目标业务用户"],
            "features": ["核心业务流程", "数据展示与交互", "响应式体验"],
            "pages": ["主工作区"],
            "acceptance_criteria": ["前后端均可运行", "核心交互可实际操作", "移动端可用"],
            "architecture": {"frontend": "HTML/CSS/JavaScript", "backend": "FastAPI JSON API", "database": "PostgreSQL project tenant schema"},
            "data_model": ["根据核心业务建立可增删改查的数据表"],
            "decisions_required": ["确认 MVP 范围后开始构建"],
            "implementation_steps": [
                {"title": "锁定产品范围与验收标准", "owner": "Emma", "deliverable": "产品规格"},
                {"title": "定义架构与租户数据模型", "owner": "Bob", "deliverable": "架构方案"},
                {"title": "实现并验证全栈体验", "owner": "Alex", "deliverable": "可运行应用"},
            ],
        }
    with SessionLocal() as db:
        run = db.get(Run, run_id)
        if run:
            run.plan = plan
            run.state = "awaiting_approval"
            project = db.get(Project, run.project_id)
            if project:
                project.status = "awaiting_approval"
            db.commit()
    add_event(run_id, "emma", "deliverable", "产品范围、用户故事和验收标准已完成", "product", {"stage": "product", "status": "completed", "features": len(plan.get("features", []))})
    add_event(run_id, "bob", "deliverable", "架构、接口边界和 PostgreSQL 租户数据模型已完成", "architecture", {"stage": "architecture", "status": "completed", "architecture": plan.get("architecture", {})})
    add_event(run_id, "mike", "plan", "产品与架构方案已汇总；批准后将交给 Alex 增量实现", "approval", plan)


def execute_tool(root: Path, name: str, args: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    if name == "list_files":
        return {"files": list_files(root)}, False
    if name == "read_file":
        path = safe_path(root, args["path"])
        if not path.is_file():
            return {"error": "file not found"}, False
        content = path.read_text(encoding="utf-8")
        return {"path": args["path"], "content": content[:50_000], "truncated": len(content) > 50_000}, False
    if name == "search_files":
        query = args["query"]
        if len(query) > 200:
            return {"error": "query too long"}, False
        base = safe_path(root, args.get("path") or ".")
        paths = [base] if base.is_file() else [path for path in base.rglob("*") if path.is_file()]
        matches = []
        try:
            pattern = re.compile(query, re.IGNORECASE)
        except re.error:
            pattern = re.compile(re.escape(query), re.IGNORECASE)
        for path in paths:
            if len(matches) >= 80 or any(part in {".atoms", "__pycache__", "node_modules"} for part in path.parts):
                continue
            try:
                for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                    if pattern.search(line):
                        matches.append({"path": path.relative_to(root).as_posix(), "line": number, "text": line[:240]})
                        if len(matches) >= 80:
                            break
            except (UnicodeDecodeError, OSError):
                continue
        return {"query": query, "matches": matches, "count": len(matches)}, False
    if name == "write_file":
        path = safe_path(root, args["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        content = args["content"]
        if len(content) > 100_000:
            return {"error": "file too large"}, False
        before = path.read_text(encoding="utf-8") if path.is_file() else ""
        path.write_text(content, encoding="utf-8")
        return {"ok": True, "path": args["path"], "bytes": len(content.encode()), "change": change_summary(before, content)}, False
    if name == "replace_text":
        path = safe_path(root, args["path"])
        if not path.is_file():
            return {"error": "file not found"}, False
        content = path.read_text(encoding="utf-8")
        old = args["old_text"]
        count = content.count(old)
        if count != 1:
            return {"error": f"old_text must occur exactly once; found {count}"}, False
        updated = content.replace(old, args["new_text"], 1)
        path.write_text(updated, encoding="utf-8")
        return {"ok": True, "path": args["path"], "bytes": len(updated.encode()), "change": change_summary(content, updated)}, False
    if name == "run_checks":
        return run_checks(root), False
    if name == "publish_project":
        return {"ok": True, "queued": True, "message": "Will deploy the verified version to Atoms Cloud"}, False
    if name == "finish":
        checks = run_checks(root)
        if not checks["ok"]:
            return {"error": "checks are not green", "checks": checks}, False
        return {"ok": True, "summary": args["summary"]}, True
    return {"error": f"unknown tool: {name}"}, False


def public_tool_event(name: str, args: dict[str, Any], result: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Keep the activity useful and small; full file contents stay in the private agent observation."""
    if name == "read_file":
        summary = f"分析 {args.get('path', '项目文件')}，确认现有实现与修改边界"
        public_result = {"path": result.get("path"), "characters": len(result.get("content", "")), "truncated": result.get("truncated", False)}
    elif name == "search_files":
        summary = f"搜索“{args.get('query', '')}”，定位到 {result.get('count', 0)} 处相关实现"
        public_result = {"query": result.get("query"), "count": result.get("count", 0), "matches": (result.get("matches") or [])[:8]}
    elif name == "list_files":
        files = result.get("files") or []
        summary = f"扫描工程结构，共 {len(files)} 个可编辑文件"
        public_result = {"count": len(files), "files": files[:30]}
    elif name in {"write_file", "replace_text"}:
        change = result.get("change") or {}
        if change.get("added", 0) == 0 and change.get("removed", 0) == 0:
            summary = f"检查 {args.get('path', '项目文件')}：本次修改没有产生差异"
        else:
            summary = f"实现 {args.get('path', '项目文件')}（+{change.get('added', 0)} −{change.get('removed', 0)}）"
        public_result = {key: result.get(key) for key in ("ok", "path", "bytes", "change", "error") if key in result}
    elif name == "run_checks":
        errors = result.get("errors") or []
        summary = "验证通过：前端结构、后端导入与租户持久化检查均正常" if result.get("ok") else f"验证发现 {len(errors)} 个问题，下一步将按失败项修复"
        public_result = {"ok": result.get("ok"), "errors": errors[:20]}
    elif name == "finish":
        summary = args.get("summary", "实现完成")
        public_result = {"ok": result.get("ok"), "summary": result.get("summary"), "error": result.get("error")}
    elif name == "publish_project":
        summary = "验证完成后将当前不可变版本发布到 Atoms Cloud"
        public_result = result
    else:
        summary, public_result = name.replace("_", " "), result
    if result.get("error"):
        summary = f"{summary}；工具返回：{result['error']}"
    return summary, public_result


async def run_builder(run_id: str) -> None:
    with SessionLocal() as db:
        run = db.get(Run, run_id)
        project = db.get(Project, run.project_id) if run else None
        if not run or not project:
            return
        goal, plan, project_id = run.goal, run.plan, project.id
    root = ensure_workspace(project_id)
    set_run_state(run_id, "building")
    add_event(run_id, "alex", "stage", "已接收请求，正在观察工程并选择下一项最小有效操作", "react", {"status": "observing", "loop": "ReAct"})
    client = RightAPIClient()
    conversation, user_cursor = pending_user_inputs(run_id, root)
    context = f"Current repository manifest:\n{project_manifest(root)}"
    if plan:
        context += f"\nOptional earlier product notes (adapt when new evidence differs):\n{json.dumps(plan, ensure_ascii=False)}"
    conversation.insert(0, {"role": "user", "content": [{"type": "input_text", "text": context}]})
    finished = False
    publish_requested = False
    final_summary = "网站已完成"
    try:
        for step in range(1, settings.max_agent_steps + 1):
            steering, user_cursor = pending_user_inputs(run_id, root, user_cursor)
            if steering:
                conversation.extend(steering)
                add_event(run_id, "alex", "message", "收到追加信息，已纳入当前 ReAct 循环", "react", {"status": "adapting", "iteration": step})
            payload: dict[str, Any] = {
                "instructions": AGENT_INSTRUCTIONS,
                "input": conversation,
                "tools": TOOLS,
                "tool_choice": "auto",
                "max_output_tokens": 10000,
            }
            response = await client.response(**payload)
            calls = [item for item in response.get("output", []) if item.get("type") == "function_call"]
            if not calls:
                text = output_text(response)
                if text:
                    add_event(run_id, "alex", "message", text[:1500], "building")
                steering, user_cursor = pending_user_inputs(run_id, root, user_cursor)
                if steering:
                    conversation.extend(steering)
                    continue
                checks = run_checks(root)
                if checks["ok"]:
                    finished = True
                    final_summary = text or final_summary
                    break
                conversation.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": f"Validation is not green. Continue using tools and repair: {json.dumps(checks)}",
                            }
                        ],
                    }
                )
                continue
            outputs = []
            for call in calls:
                conversation.append(
                    {
                        "type": "function_call",
                        "call_id": call.get("call_id"),
                        "name": call.get("name"),
                        "arguments": call.get("arguments") or "{}",
                    }
                )
                name = call.get("name", "")
                args: dict[str, Any] = {}
                try:
                    args = json.loads(call.get("arguments") or "{}")
                    result, requested_finish = execute_tool(root, name, args)
                    if name == "publish_project" and result.get("queued"):
                        publish_requested = True
                except Exception as exc:
                    result, requested_finish = {"error": str(exc)}, False
                summary, public_result = public_tool_event(name, args, result)
                add_event(run_id, "alex", "tool", summary, "implementation" if name != "run_checks" else "verification", {"tool": name, "result": public_result, "path": public_result.get("path") if isinstance(public_result, dict) else None, "iteration": step})
                outputs.append({"type": "function_call_output", "call_id": call.get("call_id"), "output": json.dumps(result, ensure_ascii=False)})
                if requested_finish:
                    finished = True
                    final_summary = args.get("summary", final_summary)
            if finished:
                break
            conversation.extend(outputs)
        if not finished:
            checks = run_checks(root)
            if not checks["ok"]:
                raise RuntimeError(f"达到最大步骤且检查未通过: {checks}")
            final_summary = "已达到步骤上限；当前版本通过固定检查并已保存"
        set_run_state(run_id, "testing")
        add_event(run_id, "alex", "deliverable", "实现阶段完成，固定检查已通过", "implementation", {"stage": "implementation", "status": "completed"})
        add_event(run_id, "system", "stage", "正在启动真实预览并保存可回溯版本", "verification", {"stage": "verification", "status": "in_progress"})
        with SessionLocal() as db:
            project = db.get(Project, project_id)
            preferred = project.preview_port if project else None
        port = await preview_manager.start(project_id, root, preferred)
        with SessionLocal() as db:
            project = db.get(Project, project_id)
            run = db.get(Run, run_id)
            count = db.scalar(select(func.count(Version.id)).where(Version.project_id == project_id)) or 0
            number = count + 1
            archive = save_snapshot(root, settings.workspace_root / "_versions" / project_id, number)
            version = Version(
                    id=str(uuid.uuid4()),
                    project_id=project_id,
                    run_id=run_id,
                    number=number,
                    label=final_summary[:150],
                    snapshot_path=str(archive),
                )
            db.add(version)
            if project:
                project.preview_port = port
                project.status = "preview_ready"
            if run:
                run.state = "preview_ready"
            db.commit()
            version_id = version.id
        add_event(run_id, "mike", "completed", final_summary, "preview", {"version": number})
        if publish_requested:
            deployment_id = str(uuid.uuid4())
            with SessionLocal() as db:
                project = db.get(Project, project_id)
                version = db.get(Version, version_id)
                db.add(Deployment(id=deployment_id, project_id=project_id, version_id=version_id, provider="atoms-cloud", status="building"))
                db.commit()
            add_event(run_id, "system", "stage", "正在由 Atoms Cloud 构建镜像并启动应用与 PostgreSQL 容器", "deployment", {"deployment_id": deployment_id, "status": "building"})
            try:
                result = await deployment_provider.publish(deployment_id, project, version)
                with SessionLocal() as db:
                    deployment = db.get(Deployment, deployment_id)
                    deployment.status, deployment.url = result.status, result.url
                    db.commit()
                add_event(run_id, "system", "deliverable", f"已发布：{result.url}", "deployment", {"deployment_id": deployment_id, "status": result.status, "url": result.url})
            except Exception as exc:
                with SessionLocal() as db:
                    deployment = db.get(Deployment, deployment_id)
                    deployment.status, deployment.error = "failed", str(exc)[:4000]
                    db.commit()
                add_event(run_id, "system", "error", f"Cloud 发布失败：{str(exc)[:1000]}", "deployment")
    except (LLMError, asyncio.TimeoutError, Exception) as exc:
        message = str(exc)[:1500]
        set_run_state(run_id, "failed", message)
        add_event(run_id, "system", "error", f"本轮执行失败：{message}", "failed")


async def begin_run(run_id: str) -> None:
    await run_builder(run_id)

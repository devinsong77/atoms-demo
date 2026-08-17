# Atoms Demo

一个可运行的 Atoms 风格 AI 产品团队 Demo。用户可以登录、创建项目、在 Team/Engineer 模式下与 Agent 协作，并让 ReAct 工程 Agent 真实生成和运行完整的 FastAPI + HTML/CSS/JavaScript 网站。

## 一键启动

1. 复制环境配置（本工作区已经配置本地 `.env`）：

   ```bash
   cp .env.example .env
   ```

2. 在 `.env` 中设置 `RIGHTAPI_API_KEY`。

3. 启动：

   ```bash
   docker compose up --build
   ```

4. 打开 <http://localhost:3000>。

停止服务：

```bash
docker compose down
```

如需同时删除平台 PostgreSQL、Cloud 元数据 PostgreSQL 与项目工作区数据：

```bash
docker compose down -v
```

项目发布使用的 `atoms-cloud-pg-*` 数据卷由 Cloud 动态创建，不属于 Compose 声明卷，因此 `down -v` 不会误删项目业务数据。需要彻底删除时应调用删除发布 API 的 `remove_volume=true`，或在确认目标项目后通过 Docker 管理工具删除对应的 managed volume。

## 测试账号

| 账号 | 密码 |
|---|---|
| `demo@atoms.local` | `demo123` |
| `founder@atoms.local` | `atoms123` |
| `builder@atoms.local` | `build123` |

测试账号首次启动时自动写入 PostgreSQL，密码使用 PBKDF2-SHA256 加盐保存。

## 已实现流程

- 本地账号登录与 Session Token。
- PostgreSQL 持久化 User、Project、Run、AgentEvent、Version、Deployment、用户偏好与项目租户映射。
- Team Mode：Mike/Emma 生成结构化产品计划，用户批准后 Alex 开始开发。
- Engineer Mode：Alex 直接进入 ReAct 工具循环。
- 原生 Responses API Function Calling：读文件、写文件、精确替换、固定检查、完成。
- 每个项目拥有独立持久化工作区，以及独立 PostgreSQL schema（`tenant_<project_uuid>`）。
- 生成应用包含真实 FastAPI JSON CRUD API 和响应式前端；禁止以 SQLite、内存或 localStorage 作为业务数据源。
- 生成应用由独立 Uvicorn 进程运行，并通过 Preview Gateway 嵌入工作台。
- Agent 事件、工具日志、代码查看、桌面/手机预览、增量修改和 ZIP 导出。
- 版本 ZIP 快照。
- All Projects、Team、Discover、Settings 均连接真实 API；模板可直接带入新项目，设置会持久化。
- 工作台支持简体中文 / English i18n、Dark / Light 主题和可读性更高的字体层级，界面图标统一使用 MIT License 的 Lucide。
- 代码页使用 MIT License 的 Monaco Editor，支持语法高亮、编辑、文件筛选、Prettier（Web 文件）与 Black（Python）格式化；保存后自动运行固定检查，通过后重启项目预览。
- Agent 文件写入通过 SSE 实时推送，文件树会标识本轮变更，活动面板展示路径、增删行数和短哈希。
- Team 工作流按 `Mike 拆解 → Emma 产品规格 → Bob 架构 → 人工审批 → Alex 增量实现 → 验证/版本` 执行；计划可在审批前退回修改。
- 独立 Atoms Cloud 控制面通过 Docker Socket 构建不可变版本镜像；每次发布启动独立应用容器，首次发布同时创建项目专属 PostgreSQL 容器/数据卷，后续版本复用数据。
- Cloud 管理面可查看发布 URL、容器状态与日志，支持启停、重启以及受项目权限保护的容器内命令执行。
- Agent 提供 `publish_project` 工具；当用户明确要求发布时，会在固定检查与版本快照完成后自动交给 Cloud。

## 架构

```text
React workspace (Nginx)
        │ /api, /preview
        ▼
FastAPI control plane ── RightAPI Responses API
        │                    │ Function Calling / ReAct
        ├── PostgreSQL       ▼
        │   ├── public：平台控制面数据
        │   └── tenant_<project_uuid>：各生成项目业务数据
        ├── Project workspace + generated FastAPI preview process
        └── Atoms Cloud API ── Docker Engine
                  │             ├── generated app container : random host port
                  │             └── dedicated PostgreSQL container + volume
                  └── Cloud PostgreSQL（发布元数据）
```

## PostgreSQL 项目租户

平台只使用 Compose 中的 PostgreSQL，不包含 SQLite。创建项目时，控制面会在同一个数据库事务中建立 `project_tenants` 映射和对应 schema；启动时也会为历史项目补齐 schema。生成应用通过平台注入的 `PROJECT_DATABASE_URL` 和 `PROJECT_TENANT_SCHEMA` 连接数据库，并在自己的 schema 中建表。Agent 的固定检查会拒绝 SQLite、缺少租户变量、没有写 API 或前端未调用写接口的结果。

该方案让单个 Compose 保持一键部署，同时保证项目间表名和业务数据隔离。它是 schema 级多租户；若面向不可信的公网租户，还应继续增加独立数据库角色、行级权限、连接配额和 Sandbox 网络策略。

## 端到端验证

运行可重复的 PostgreSQL 租户测试：

```bash
docker compose run --rm backend python -m unittest tests.test_generated_template -v
docker compose run --rm backend python -m unittest tests.test_cloud_provider -v
```

测试覆盖生成模板契约、真实 CRUD、重新建立客户端后的持久化，以及两个项目 schema 之间的数据隔离。人工浏览器验收清单见 `tests/e2e/README.md`。

预览仍运行在 backend 容器的非 root 用户中；正式发布则进入独立容器。Cloud 挂载 Docker Socket，等同于拥有宿主 Docker 管理权限，只适合可信的本地开发环境。面向不可信公网用户时应改为受限的远程 Worker/Kubernetes API，并增加镜像扫描、CPU/内存/磁盘配额、网络策略和命令审计。

## 开源项目参考与取舍

- **MetaGPT**：参考 Team Leader 中央路由、Emma/Alex 角色分工、结构化 Product Plan 和显式 Artifact/SOP；Demo 用可恢复的 `Project → Run → AgentEvent → Version` 数据模型承接产品状态。
- **OpenManus**：参考 ReAct、Function Calling、Tool Collection 和“观察工具结果后继续行动”的循环；Demo 只暴露受控的文件读写、精确替换和固定检查工具，不沿用默认 Host Python 执行方式。
- **Codex / Claude Code 类体验**：Agent 先扫描和读取现有文件，再增量修改、运行真实检查、根据 Observation 修复，最终明确 Finish；工作台只展示工具与公开摘要，不展示私有推理过程。

主要开源组件包括 React、Vite、Lucide、FastAPI、SQLAlchemy、PostgreSQL、Nginx 和 Docker Compose。MetaGPT/OpenManus 在此作为架构参考，没有把两套框架的 Memory、Message 和 Tool Runtime 直接拼接进产品。

## Atoms Cloud 发布与容器管理

`cloud/` 是独立 FastAPI 模块，拥有自己的 PostgreSQL 元数据数据库。`POST /api/projects/{id}/publish` 将最新 `Version` ZIP 快照交给 Cloud。每个项目始终只有一套当前运行栈；重新发布会删除旧前后端实例并启动：

- `atoms-frontend-<project>`：Nginx 静态前端与 `/api` 反向代理，对宿主发布端口。
- `atoms-backend-<project>`：生成的 FastAPI API，仅在项目网络内提供服务。
- `atoms-db-<project>`：该项目独享的 PostgreSQL 16；重新发布复用命名数据卷，不使用 SQLite。

动态容器默认限制 CPU、内存和 PID 数量。Cloud 从可配置的 `20000-45000` 端口范围选择宿主端口，restart 或 stop/start 不会改变当前 URL。一键下线会删除项目的全部容器但保留命名数据卷；永久删除归档项目时同时删除数据卷、租户 schema 和工程文件。

管理 API 只返回当前登录用户项目下、带 `atoms.cloud.managed=true` 标签的容器。前端 Cloud 页按项目聚合服务，日志面板只显示日志；终端使用 shell 字符串并通过受控 `docker exec /bin/sh -lc` 执行。Cloud 内部 API 使用 `X-Cloud-Key` 隔离，不直接暴露控制端口。

一键部署仍是 `docker compose up --build`。Compose 会同时启动平台 DB、Cloud 元数据 DB、Cloud API、主后端和前端。生成应用的随机宿主端口会显示在 Cloud 页。Windows/macOS 的 Docker Desktop 需要允许 Linux 容器访问 Docker Socket。

## 本地开发

后端：

```bash
python -m venv .venv
.venv/Scripts/pip install -r backend/requirements.txt
uvicorn backend.app.main:app --reload --port 8000
```

前端：

```bash
cd frontend
npm install
npm run dev
```

本地开发时需提供可访问的 PostgreSQL，并将 `DATABASE_URL` 指向它。

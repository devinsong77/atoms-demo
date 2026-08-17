import asyncio
import os
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import settings


@dataclass
class PreviewProcess:
    process: subprocess.Popen
    port: int
    log_path: Path


class PreviewManager:
    def __init__(self) -> None:
        self.processes: dict[str, PreviewProcess] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def port_available(port: int) -> bool:
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return True
            except OSError:
                return False

    @staticmethod
    def free_port(start: int) -> int:
        for port in range(start, start + 800):
            if PreviewManager.port_available(port):
                return port
        raise RuntimeError("没有可用的预览端口")

    async def start(self, project_id: str, root: Path, preferred_port: int | None = None) -> int:
        async with self._lock:
            current = self.processes.get(project_id)
            if current and current.process.poll() is None:
                current.process.terminate()
                try:
                    current.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    current.process.kill()
            live_ports = {item.port for item in self.processes.values() if item.process.poll() is None}
            if preferred_port and preferred_port not in live_ports and self.port_available(preferred_port):
                port = preferred_port
            else:
                port = self.free_port(settings.preview_port_start)
            log_path = root / ".atoms" / "preview.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("ab") as log_file:
                preview_env = os.environ.copy()
                preview_env["PROJECT_DATABASE_URL"] = settings.database_url.replace("postgresql+psycopg2://", "postgresql://")
                preview_env["PROJECT_TENANT_SCHEMA"] = f"tenant_{project_id.replace('-', '')}"
                process = subprocess.Popen(
                    [sys.executable, "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", str(port)],
                    cwd=root,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    env=preview_env,
                )
            self.processes[project_id] = PreviewProcess(process=process, port=port, log_path=log_path)
        async with httpx.AsyncClient() as client:
            for _ in range(40):
                if process.poll() is not None:
                    detail = log_path.read_text(encoding="utf-8", errors="replace")[-3000:]
                    raise RuntimeError(f"预览进程启动失败: {detail}")
                try:
                    response = await client.get(f"http://127.0.0.1:{port}/api/health", timeout=1)
                    if response.status_code == 200:
                        return port
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.25)
        process.terminate()
        raise RuntimeError("预览服务健康检查超时")

    def port_for(self, project_id: str) -> int | None:
        item = self.processes.get(project_id)
        if item and item.process.poll() is None:
            return item.port
        return None

    def stop(self, project_id: str) -> None:
        item = self.processes.pop(project_id, None)
        if item and item.process.poll() is None:
            item.process.terminate()

    def stop_all(self) -> None:
        for item in self.processes.values():
            if item.process.poll() is None:
                item.process.terminate()


preview_manager = PreviewManager()

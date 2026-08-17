from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .config import settings
from .models import Project, Version


class CloudProviderError(RuntimeError):
    pass


@dataclass
class DeploymentResult:
    provider: str
    status: str
    url: str | None = None
    detail: dict[str, Any] | None = None


class AtomsCloudProvider:
    provider_name = "atoms-cloud"

    def __init__(self) -> None:
        self.base_url = settings.cloud_base_url.rstrip("/")
        self.headers = {"X-Cloud-Key": settings.cloud_api_key}

    async def _request(self, method: str, path: str, **kwargs) -> Any:
        try:
            async with httpx.AsyncClient(timeout=settings.cloud_timeout_seconds) as client:
                response = await client.request(method, f"{self.base_url}{path}", headers=self.headers, **kwargs)
        except httpx.HTTPError as exc:
            raise CloudProviderError(f"Atoms Cloud unavailable: {exc}") from exc
        if not response.is_success:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            raise CloudProviderError(str(detail))
        return None if response.status_code == 204 else response.json()

    async def publish(self, deployment_id: str, project: Project, version: Version) -> DeploymentResult:
        snapshot = Path(version.snapshot_path).resolve()
        try:
            relative = snapshot.relative_to(settings.workspace_root.resolve()).as_posix()
        except ValueError as exc:
            raise CloudProviderError("Version snapshot is outside the shared workspace") from exc
        detail = await self._request("POST", "/deployments", json={
            "deployment_id": deployment_id,
            "project_id": project.id,
            "project_name": project.name,
            "version_number": version.number,
            "snapshot_path": relative,
        })
        return DeploymentResult(self.provider_name, detail["status"], detail.get("public_url"), detail)

    async def deployments(self, project_id: str | None = None) -> list[dict]:
        return await self._request("GET", "/deployments", params={"project_id": project_id} if project_id else None)

    async def deployment(self, deployment_id: str) -> dict:
        return await self._request("GET", f"/deployments/{deployment_id}")

    async def delete(self, deployment_id: str, remove_volume: bool = False) -> None:
        await self._request("DELETE", f"/deployments/{deployment_id}", params={"remove_volume": str(remove_volume).lower()})

    async def containers(self, deployment_id: str | None = None) -> list[dict]:
        return await self._request("GET", "/containers", params={"deployment_id": deployment_id} if deployment_id else None)

    async def logs(self, container_id: str, tail: int = 300) -> dict:
        return await self._request("GET", f"/containers/{container_id}/logs", params={"tail": tail})

    async def action(self, container_id: str, action: str) -> dict:
        return await self._request("POST", f"/containers/{container_id}/action", json={"action": action})

    async def exec(self, container_id: str, command: str) -> dict:
        return await self._request("POST", f"/containers/{container_id}/exec", json={"command": command})

    async def offline_project(self, project_id: str, remove_volume: bool = False) -> None:
        await self._request("DELETE", f"/projects/{project_id}/stack", params={"remove_volume": str(remove_volume).lower()})


deployment_provider = AtomsCloudProvider()

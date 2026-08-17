import json
from typing import Any

import httpx

from .config import settings


class LLMError(RuntimeError):
    pass


class RightAPIClient:
    def __init__(self) -> None:
        self.base_url = settings.rightapi_base_url.rstrip("/")
        self.api_key = settings.rightapi_api_key
        self.model = settings.rightapi_model

    async def response(self, **payload: Any) -> dict[str, Any]:
        if not self.api_key:
            raise LLMError("RIGHTAPI_API_KEY 尚未配置")
        body = {"model": self.model, **payload}
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=httpx.Timeout(180.0)) as client:
            result = await client.post(f"{self.base_url}/responses", headers=headers, json=body)
        if result.status_code >= 400:
            detail = result.text[:1000]
            raise LLMError(f"LLM 请求失败 ({result.status_code}): {detail}")
        return result.json()


def output_text(response: dict[str, Any]) -> str:
    chunks: list[str] = []
    for item in response.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") == "output_text" and content.get("text"):
                chunks.append(content["text"])
    return "\n".join(chunks).strip()


def parse_json_text(text: str) -> dict[str, Any]:
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.split("\n", 1)[-1]
        candidate = candidate.rsplit("```", 1)[0]
    return json.loads(candidate)

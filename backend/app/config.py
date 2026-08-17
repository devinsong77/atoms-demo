from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg2://atoms:atoms-local-password@localhost:5432/atoms"
    rightapi_base_url: str = "https://www.rightapi.ai/codex/v1"
    rightapi_api_key: str = ""
    rightapi_model: str = "gpt-5.6-terra"
    session_ttl_hours: int = 168
    max_agent_steps: int = 24
    max_run_seconds: int = 600
    preview_port_start: int = 9300
    public_app_url: str = "http://localhost:5173"
    cloud_base_url: str = "http://cloud:8100"
    cloud_api_key: str = "atoms-cloud-local-key"
    cloud_timeout_seconds: int = 900
    workspace_root: Path = Path(__file__).resolve().parents[2] / "workspaces"

    model_config = SettingsConfigDict(env_file=(".env", "../.env"), extra="ignore")


settings = Settings()
settings.workspace_root.mkdir(parents=True, exist_ok=True)

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class LoginRequest(BaseModel):
    email: str
    password: str


class ProjectCreate(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    description: str = Field(min_length=3, max_length=4000)
    mode: Literal["engineer", "team"] = "team"
    icon_data_url: str | None = Field(default=None, max_length=750_000)


class AttachmentInput(BaseModel):
    name: str = Field(min_length=1, max_length=180)
    mime: str = Field(min_length=1, max_length=120)
    data_url: str = Field(min_length=1, max_length=12_000_000)


class MessageCreate(BaseModel):
    content: str = Field(default="", max_length=12000)
    attachments: list[AttachmentInput] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def require_content_or_attachment(self):
        if not self.content.strip() and not self.attachments:
            raise ValueError("消息或附件不能为空")
        return self


class FileUpdate(BaseModel):
    path: str = Field(min_length=1, max_length=500)
    content: str = Field(max_length=200_000)


class ModeUpdate(BaseModel):
    mode: Literal["engineer", "team"]


class SettingsUpdate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    default_mode: Literal["engineer", "team"] = "team"
    compact_events: bool = False
    locale: Literal["zh-CN", "en-US"] = "zh-CN"
    theme: Literal["light", "dark"] = "light"


class CloudContainerAction(BaseModel):
    action: Literal["start", "stop", "restart"]


class CloudExecRequest(BaseModel):
    command: str = Field(min_length=1, max_length=4000)

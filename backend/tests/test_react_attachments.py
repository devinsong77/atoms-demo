import base64
import shutil
import uuid
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.agent import begin_run
from app.db import SessionLocal
from app.main import app
from app.models import AgentEvent, Project, Run, User
from app.workspace import project_root


class ReactAttachmentE2ETest(unittest.TestCase):
    def setUp(self):
        self.project_id = str(uuid.uuid4())
        self.run_id = str(uuid.uuid4())
        with SessionLocal() as db:
            user = db.scalar(select(User).where(User.email == "demo@atoms.local"))
            db.add(Project(id=self.project_id, owner_id=user.id, name="Attachment E2E", description="test", mode="team", status="building"))
            db.add(Run(id=self.run_id, project_id=self.project_id, goal="existing task", mode="team", state="building"))
            db.commit()
        self.client = TestClient(app)
        response = self.client.post("/api/auth/login", json={"email": "demo@atoms.local", "password": "demo123"})
        self.token = response.json()["token"]
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def tearDown(self):
        with SessionLocal() as db:
            project = db.get(Project, self.project_id)
            if project:
                db.delete(project)
                db.commit()
        shutil.rmtree(project_root(self.project_id), ignore_errors=True)

    def test_active_run_accepts_text_and_document_as_steering(self):
        encoded = base64.b64encode(b"acceptance criteria from attachment").decode()
        response = self.client.post(
            f"/api/projects/{self.project_id}/messages",
            headers=self.headers,
            json={"content": "Use this additional context", "attachments": [{"name": "brief.txt", "mime": "text/plain", "data_url": f"data:text/plain;base64,{encoded}"}]},
        )
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["id"], self.run_id)
        with SessionLocal() as db:
            event = db.scalar(select(AgentEvent).where(AgentEvent.run_id == self.run_id, AgentEvent.agent == "user"))
            attachment = event.payload["attachments"][0]
            self.assertTrue(event.payload["steer"])
        downloaded = self.client.get(f"/api/projects/{self.project_id}/attachments/{attachment['id']}?token={self.token}")
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.content, b"acceptance criteria from attachment")


class ReactDispatchTest(unittest.IsolatedAsyncioTestCase):
    async def test_every_mode_dispatches_directly_to_react_builder(self):
        with patch("app.agent.run_builder", new=AsyncMock()) as builder:
            await begin_run("any-run")
            builder.assert_awaited_once_with("any-run")


if __name__ == "__main__":
    unittest.main()

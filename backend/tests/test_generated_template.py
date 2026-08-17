import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

import psycopg2
from fastapi.testclient import TestClient
from psycopg2 import sql

from app.config import settings
from app.workspace import STARTER_FILES, run_checks


class GeneratedTemplateE2ETest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="atoms-template-e2e-"))
        self.project_id = str(uuid.uuid4())
        self.root = self.temp_dir / self.project_id
        self.schema = f"tenant_{self.project_id.replace('-', '')}"
        self.schemas = [self.schema]
        self.database_url = settings.database_url.replace("postgresql+psycopg2://", "postgresql://")
        self.create_workspace(self.root)
        os.environ["PROJECT_DATABASE_URL"] = self.database_url
        os.environ["PROJECT_TENANT_SCHEMA"] = self.schema

    @staticmethod
    def create_workspace(root: Path):
        for relative, content in STARTER_FILES.items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

    def tearDown(self):
        with psycopg2.connect(self.database_url) as connection:
            with connection.cursor() as cursor:
                for schema_name in self.schemas:
                    cursor.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema_name)))
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def load_generated_app(self, project_id: str | None = None, root: Path | None = None, schema_name: str | None = None):
        project_id = project_id or self.project_id
        root = root or self.root
        schema_name = schema_name or self.schema
        os.environ["PROJECT_TENANT_SCHEMA"] = schema_name
        module_name = f"generated_{project_id.replace('-', '')}"
        spec = importlib.util.spec_from_file_location(module_name, root / "backend" / "main.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module.app

    def test_postgresql_tenant_crud_survives_new_client(self):
        checks = run_checks(self.root)
        self.assertTrue(checks["ok"], checks)
        app = self.load_generated_app()
        first_client = TestClient(app)
        initial = first_client.get("/api/tasks")
        self.assertEqual(initial.status_code, 200)
        created = first_client.post("/api/tasks", json={"title": "Persistent E2E task", "priority": "high"})
        self.assertEqual(created.status_code, 201)
        task_id = created.json()["id"]
        updated = first_client.patch(f"/api/tasks/{task_id}", json={"done": True})
        self.assertEqual(updated.status_code, 200)
        self.assertTrue(updated.json()["done"])

        second_client = TestClient(app)
        persisted = second_client.get("/api/tasks").json()
        self.assertTrue(any(row["id"] == task_id and row["done"] for row in persisted))
        self.assertEqual(second_client.delete(f"/api/tasks/{task_id}").status_code, 204)
        self.assertFalse(any(row["id"] == task_id for row in second_client.get("/api/tasks").json()))

    def test_two_project_schemas_are_isolated(self):
        second_project_id = str(uuid.uuid4())
        second_root = self.temp_dir / second_project_id
        second_schema = f"tenant_{second_project_id.replace('-', '')}"
        self.schemas.append(second_schema)
        self.create_workspace(second_root)

        first_client = TestClient(self.load_generated_app())
        created = first_client.post("/api/tasks", json={"title": "Tenant A exclusive", "priority": "high"})
        self.assertEqual(created.status_code, 201)

        second_client = TestClient(self.load_generated_app(second_project_id, second_root, second_schema))
        second_titles = [row["title"] for row in second_client.get("/api/tasks").json()]
        self.assertNotIn("Tenant A exclusive", second_titles)

        os.environ["PROJECT_TENANT_SCHEMA"] = self.schema
        first_titles = [row["title"] for row in first_client.get("/api/tasks").json()]
        self.assertIn("Tenant A exclusive", first_titles)


if __name__ == "__main__":
    unittest.main()

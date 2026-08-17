import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import deployments
from app.agent import TOOLS, execute_tool, public_tool_event


class CloudProviderTests(unittest.TestCase):
    def setUp(self):
        self.old_root = deployments.settings.workspace_root
        self.temp = tempfile.TemporaryDirectory()
        deployments.settings.workspace_root = Path(self.temp.name)
        self.snapshot = Path(self.temp.name) / "_versions" / "project" / "v1.zip"
        self.snapshot.parent.mkdir(parents=True)
        self.snapshot.write_bytes(b"snapshot")

    def tearDown(self):
        deployments.settings.workspace_root = self.old_root
        self.temp.cleanup()

    def test_publish_sends_shared_relative_snapshot(self):
        provider = deployments.AtomsCloudProvider()
        provider._request = AsyncMock(return_value={"status": "running", "public_url": "http://localhost:49152"})
        project = SimpleNamespace(id="11111111-1111-1111-1111-111111111111", name="Cloud App")
        version = SimpleNamespace(snapshot_path=str(self.snapshot), number=1)

        result = asyncio.run(provider.publish("22222222-2222-2222-2222-222222222222", project, version))

        self.assertEqual(result.status, "running")
        payload = provider._request.await_args.kwargs["json"]
        self.assertEqual(payload["snapshot_path"], "_versions/project/v1.zip")
        self.assertEqual(payload["project_id"], project.id)

    def test_publish_rejects_snapshot_outside_workspace(self):
        provider = deployments.AtomsCloudProvider()
        project = SimpleNamespace(id="11111111-1111-1111-1111-111111111111", name="Cloud App")
        version = SimpleNamespace(snapshot_path=str(Path(self.temp.name).parent / "outside.zip"), number=1)
        with self.assertRaises(deployments.CloudProviderError):
            asyncio.run(provider.publish("22222222-2222-2222-2222-222222222222", project, version))

    def test_cloud_http_error_is_normalized(self):
        provider = deployments.AtomsCloudProvider()
        response = SimpleNamespace(is_success=False, status_code=500, text="failed", json=lambda: {"detail": "image build failed"})
        client = AsyncMock()
        client.request.return_value = response
        context = AsyncMock()
        context.__aenter__.return_value = client
        with patch.object(deployments.httpx, "AsyncClient", return_value=context):
            with self.assertRaisesRegex(deployments.CloudProviderError, "image build failed"):
                asyncio.run(provider._request("GET", "/deployments"))

    def test_agent_exposes_cloud_publish_tool(self):
        names = {tool["name"] for tool in TOOLS}
        self.assertIn("publish_project", names)
        self.assertIn("search_files", names)
        result, requested_finish = execute_tool(Path(self.temp.name), "publish_project", {})
        self.assertTrue(result["queued"])
        self.assertFalse(requested_finish)

    def test_search_observation_is_useful_and_file_contents_are_not_public(self):
        source = Path(self.temp.name) / "frontend" / "app.js"
        source.parent.mkdir()
        source.write_text("const project = 'atoms'\n", encoding="utf-8")
        result, _ = execute_tool(Path(self.temp.name), "search_files", {"query": "project", "path": "."})
        summary, public = public_tool_event("search_files", {"query": "project", "path": "."}, result)
        self.assertEqual(public["count"], 1)
        self.assertIn("1", summary)
        read_result, _ = execute_tool(Path(self.temp.name), "read_file", {"path": "frontend/app.js"})
        _, public_read = public_tool_event("read_file", {"path": "frontend/app.js"}, read_result)
        self.assertNotIn("content", public_read)


if __name__ == "__main__":
    unittest.main()

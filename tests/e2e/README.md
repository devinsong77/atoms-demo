# End-to-end verification

Run the reproducible PostgreSQL tenant/template contract test:

```bash
docker compose build backend
docker compose run --rm backend python -m unittest tests.test_generated_template -v
```

The test creates a fresh project tenant schema, validates the generated application contract, performs create/update/read/delete through its FastAPI API, creates a new client, verifies persistence, and drops the temporary schema.

Platform UI coverage verified against `http://localhost:3000`:

1. Login with a seeded PostgreSQL account.
2. Open All projects and select a project.
3. Open Team and verify AI agents plus database-backed workspace members.
4. Open Discover, filter templates, and confirm Use template prefills the project brief.
5. Open Settings, save a profile/default-mode change, reload, and verify persistence.
6. Generate a project, wait for `preview_ready`, create/update/delete a record in the generated UI, and reload the page to verify PostgreSQL persistence.
7. Start two project previews after a backend restart and assert that their page identities and ports remain distinct.
8. Switch Chinese/English and light/dark modes, reload, and verify the selected appearance persists.
9. Open Code, edit a generated project file in Monaco, format it, save it, and verify fixed checks plus preview restart.
10. Start a Team run and verify live stages for Mike, Emma, Bob, approval, Alex implementation and verification; confirm changed files appear without a manual refresh.
11. Create a project with an uploaded PNG/JPEG/WebP icon and verify it appears in the sidebar and workspace header; create another without an upload and verify the first-character fallback.
12. Archive a project, verify it disappears from active projects and appears under Settings, then restore it; archive again and verify permanent deletion removes it.
13. Publish a versioned project and verify Cloud shows exactly one project stack with separate frontend, backend and PostgreSQL containers.
14. Open the published URL and `/api/health`; verify Logs is read-only and separate from Terminal, run `printf cloud-exec-ok && pwd` as a shell string, then stop/start/restart the frontend.
15. Redeploy and verify the old frontend/backend container IDs are gone, PostgreSQL data persists, and only one stack remains. Use one-click offline and verify every project container is removed.

The Cloud portion is automated for a project that already has a successful version:

```powershell
./tests/e2e/cloud-smoke.ps1 -ProjectId <project-uuid>
```

This script performs a real image build and deployment, verifies the frontend/backend/database split, reads logs, runs a shell command inside the backend container, tests restart plus stop/start, and proves the published URL stays stable. It leaves the deployment running for UI inspection.

Add `-VerifyRedeployPersistence` to prove a new publish replaces the old frontend/backend while retaining the PostgreSQL marker. Add `-VerifyOffline` to delete every project container through the one-click offline API and republish a fresh stack afterward.

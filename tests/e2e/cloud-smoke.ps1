param(
  [Parameter(Mandatory = $true)][string]$ProjectId,
  [string]$BaseUrl = "http://localhost:3000",
  [string]$Email = "demo@atoms.local",
  [string]$Password = "demo123",
  [switch]$VerifyRedeployPersistence,
  [switch]$VerifyOffline
)

$ErrorActionPreference = "Stop"
$login = Invoke-RestMethod -Method Post -Uri "$BaseUrl/api/auth/login" -ContentType "application/json" -Body (@{ email = $Email; password = $Password } | ConvertTo-Json)
$headers = @{ Authorization = "Bearer $($login.token)" }
$deployment = Invoke-RestMethod -Method Post -Uri "$BaseUrl/api/projects/$ProjectId/publish" -Headers $headers
if ($deployment.status -ne "running" -or -not $deployment.url) { throw "Deployment did not reach running state" }

$health = Invoke-RestMethod -Uri "$($deployment.url)/api/health"
if ($health.status -ne "ok") { throw "Published application health check failed" }

$frontendId = $deployment.frontend_container
$backendId = $deployment.backend_container
$databaseId = $deployment.db_container
if (-not $frontendId -or -not $backendId -or -not $databaseId) { throw "Expected frontend, backend and PostgreSQL containers" }
$serviceIds = @($frontendId, $backendId, $databaseId) | Select-Object -Unique
if ($serviceIds.Count -ne 3) { throw "Project stack services are not isolated containers" }

$logs = Invoke-RestMethod -Uri "$BaseUrl/api/cloud/containers/$backendId/logs?tail=100" -Headers $headers
if (-not $logs.logs) { throw "Application log stream is empty" }

$execBody = @{ command = "printf cloud-exec-ok && pwd" } | ConvertTo-Json
$execResult = Invoke-RestMethod -Method Post -Uri "$BaseUrl/api/cloud/containers/$backendId/exec" -Headers $headers -ContentType "application/json" -Body $execBody
if ($execResult.exit_code -ne 0 -or $execResult.stdout -notmatch "cloud-exec-ok") { throw "Container exec failed" }

if ($VerifyRedeployPersistence) {
  $markerSql = "CREATE TABLE IF NOT EXISTS public.atoms_cloud_smoke(value TEXT PRIMARY KEY); INSERT INTO public.atoms_cloud_smoke(value) VALUES ('persists') ON CONFLICT DO NOTHING;"
  $markerBody = @{ command = "psql -U app -d app -c `"$markerSql`"" } | ConvertTo-Json
  $marker = Invoke-RestMethod -Method Post -Uri "$BaseUrl/api/cloud/containers/$databaseId/exec" -Headers $headers -ContentType "application/json" -Body $markerBody
  if ($marker.exit_code -ne 0) { throw "Could not write persistence marker to project PostgreSQL" }

  $secondDeployment = Invoke-RestMethod -Method Post -Uri "$BaseUrl/api/projects/$ProjectId/publish" -Headers $headers
  if ($secondDeployment.db_container -ne $databaseId) { throw "Redeployment did not reuse the project PostgreSQL container" }
  if ($secondDeployment.frontend_container -eq $frontendId -or $secondDeployment.backend_container -eq $backendId) { throw "Redeployment did not replace old frontend/backend containers" }
  $readBody = @{ command = "psql -U app -d app -tAc `"SELECT COUNT(*) FROM public.atoms_cloud_smoke WHERE value='persists'`"" } | ConvertTo-Json
  $readResult = Invoke-RestMethod -Method Post -Uri "$BaseUrl/api/cloud/containers/$databaseId/exec" -Headers $headers -ContentType "application/json" -Body $readBody
  if ($readResult.exit_code -ne 0 -or $readResult.stdout.Trim() -ne "1") { throw "Project PostgreSQL data did not persist across deployments" }
}

$activeFrontendId = if ($secondDeployment) { $secondDeployment.frontend_container } else { $frontendId }
$activeUrl = if ($secondDeployment) { $secondDeployment.url } else { $deployment.url }
$after = Invoke-RestMethod -Method Post -Uri "$BaseUrl/api/cloud/containers/$activeFrontendId/action" -Headers $headers -ContentType "application/json" -Body '{"action":"restart"}'
if ($after.status -ne "running") { throw "Application container did not recover after restart" }
$recoveredUrl = if ($after.public_url) { $after.public_url } else { $activeUrl }
if ($recoveredUrl -ne $activeUrl) { throw "Published URL changed after restart" }
$recoveredHealth = Invoke-RestMethod -Uri "$recoveredUrl/api/health"
if ($recoveredHealth.status -ne "ok") { throw "Application URL did not recover after restart" }

$stopped = Invoke-RestMethod -Method Post -Uri "$BaseUrl/api/cloud/containers/$activeFrontendId/action" -Headers $headers -ContentType "application/json" -Body '{"action":"stop"}'
if ($stopped.status -ne "exited") { throw "Application container did not stop" }
$started = Invoke-RestMethod -Method Post -Uri "$BaseUrl/api/cloud/containers/$activeFrontendId/action" -Headers $headers -ContentType "application/json" -Body '{"action":"start"}'
if ($started.status -ne "running" -or $started.public_url -ne $activeUrl) { throw "Frontend container did not restart on its stable URL" }
$startedHealth = Invoke-RestMethod -Uri "$($started.public_url)/api/health"
if ($startedHealth.status -ne "ok") { throw "Stable URL did not recover after stop/start" }

if ($VerifyOffline) {
  Invoke-WebRequest -Method Delete -Uri "$BaseUrl/api/cloud/projects/$ProjectId/stack" -Headers $headers | Out-Null
  $remaining = @(docker ps -a --filter "label=atoms.cloud.project_id=$ProjectId" --format '{{.ID}}')
  if ($remaining.Count -ne 0) { throw "One-click offline left project containers behind" }
  $restored = Invoke-RestMethod -Method Post -Uri "$BaseUrl/api/projects/$ProjectId/publish" -Headers $headers
  if ($restored.status -ne "running") { throw "Project could not be republished after offline" }
  $activeUrl = $restored.url
}

Write-Host "Cloud smoke test passed: $activeUrl"
Write-Host "Frontend container: $activeFrontendId"
Write-Host "Backend container: $backendId"
Write-Host "PostgreSQL container: $databaseId"

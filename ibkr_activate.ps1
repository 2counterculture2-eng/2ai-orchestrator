# IBKR activation script
# Run this when IBKR account is approved and funded.
# Creates Railway IB Gateway service and configures the orchestrator.
# Usage: .\ibkr_activate.ps1 -AccountId "U26600350"

param(
    [string]$AccountId = "U26600350",
    [string]$RailwayToken = "1cb1c348-5358-4620-99bb-4a2e17a7984c",
    [string]$ProjectId = "a9375e6d-1f7d-47aa-94f3-dd70f2e0b50e",
    [string]$OrchestratorServiceId = "155db9ac-abb9-408b-8bea-00b51b8a02c7",
    [string]$EnvId = "f23ef4f6-5a1f-46b3-98f9-8f5eacf2f45c"
)

$headers = @{
    "Authorization" = "Bearer $RailwayToken"
    "Content-Type"  = "application/json"
}

Write-Host "[1/4] Creating IB Gateway Railway service..."
$createQ = @{query = 'mutation { serviceCreate(input: { projectId: "' + $ProjectId + '", name: "ib-gateway" }) { id name } }'}
$svcResp = Invoke-RestMethod -Uri "https://backboard.railway.app/graphql/v2" -Method POST -Headers $headers -Body ($createQ | ConvertTo-Json)
$ibGwServiceId = $svcResp.data.serviceCreate.id
if (-not $ibGwServiceId) { Write-Host "ERROR: Could not create service"; exit 1 }
Write-Host "  Service created: $ibGwServiceId"

Write-Host "[2/4] Setting IB Gateway environment variables..."
$ibVars = @{
    "IBEAM_ACCOUNT"      = "takuma2ai9"
    "IBEAM_PASSWORD"     = "Ibkr2AI2025!"
    "IBEAM_TOTP_SECRET"  = "5Z2SCOGHM4BU67KZ2BNSJL2ZTIVEMCGE"
    "IBEAM_REQUEST_RETRIES" = "5"
    "PORT"               = "5000"
}
foreach ($kv in $ibVars.GetEnumerator()) {
    $varQ = @{query = 'mutation { variableUpsert(input: { projectId: "' + $ProjectId + '", serviceId: "' + $ibGwServiceId + '", environmentId: "' + $EnvId + '", name: "' + $kv.Key + '", value: "' + $kv.Value + '" }) }'}
    Invoke-RestMethod -Uri "https://backboard.railway.app/graphql/v2" -Method POST -Headers $headers -Body ($varQ | ConvertTo-Json) | Out-Null
    Write-Host "  Set $($kv.Key)"
}

Write-Host "[3/4] Linking GitHub repo (ib-gateway subdirectory)..."
$linkQ = @{query = 'mutation { serviceConnect(id: "' + $ibGwServiceId + '", input: { repo: "2counterculture2-eng/2ai-orchestrator", branch: "master", rootDirectory: "ib-gateway" }) { id } }'}
Invoke-RestMethod -Uri "https://backboard.railway.app/graphql/v2" -Method POST -Headers $headers -Body ($linkQ | ConvertTo-Json) | Out-Null
Write-Host "  GitHub linked"

Write-Host "[4/4] Updating orchestrator env vars (IBKR_GATEWAY_URL + IBKR_ACCOUNT_ID)..."
Start-Sleep -Seconds 30
$ibGwUrl = "https://ib-gateway.railway.internal:5000/v1/api"
$orchVars = @{
    "IBKR_GATEWAY_URL" = $ibGwUrl
    "IBKR_ACCOUNT_ID"  = $AccountId
    "IBKR_PAPER"       = "true"
}
foreach ($kv in $orchVars.GetEnumerator()) {
    $varQ = @{query = 'mutation { variableUpsert(input: { projectId: "' + $ProjectId + '", serviceId: "' + $OrchestratorServiceId + '", environmentId: "' + $EnvId + '", name: "' + $kv.Key + '", value: "' + $kv.Value + '" }) }'}
    Invoke-RestMethod -Uri "https://backboard.railway.app/graphql/v2" -Method POST -Headers $headers -Body ($varQ | ConvertTo-Json) | Out-Null
    Write-Host "  Set $($kv.Key)"
}

$sha = git rev-parse HEAD
$deployQ = 'mutation { serviceInstanceDeployV2(serviceId: "' + $OrchestratorServiceId + '", environmentId: "' + $EnvId + '", commitSha: "' + $sha + '") }'
Invoke-RestMethod -Uri "https://backboard.railway.app/graphql/v2" -Method POST -Headers $headers -Body (@{query=$deployQ} | ConvertTo-Json) | Out-Null
Write-Host "  Orchestrator redeployed"

Write-Host ""
Write-Host "=== IBKR ACTIVATION COMPLETE ==="
Write-Host "IB Gateway service ID: $ibGwServiceId"
Write-Host "Gateway URL: $ibGwUrl"
Write-Host "Account ID: $AccountId"
Write-Host "Wait 2-3 minutes for IB Gateway to authenticate, then verify:"
Write-Host "  curl https://orchestrator-production-61d8.up.railway.app/status"

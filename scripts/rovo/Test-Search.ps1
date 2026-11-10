<#
.SYNOPSIS
Verifies a Rovo scoped API token can call the generic cross-product `search` MCP tool.

.PARAMETER Token
The scoped API token. If omitted, reads $env:ROVO_API_TOKEN.

.PARAMETER Query
Free-text search query (1-1000 chars).

.PARAMETER TargetApp
One of ALL, JIRA, CONFLUENCE, THIRD_PARTY. Defaults to ALL.

.PARAMETER SiteUrl
Your Atlassian tenant URL, used only to resolve the cloudId.

.EXAMPLE
$env:ROVO_API_TOKEN = '<token>'
.\Test-Search.ps1 -Query 'onboarding'

.EXAMPLE
.\Test-Search.ps1 -Token '<token>' -Query 'PROJ-123' -TargetApp JIRA
#>
param(
    [string]$Token,
    [Parameter(Mandatory)] [string]$Query,
    [ValidateSet('ALL', 'JIRA', 'CONFLUENCE', 'THIRD_PARTY')] [string]$TargetApp = 'ALL',
    [string]$SiteUrl = 'https://yourcompany.atlassian.net'
)

. "$PSScriptRoot\RovoMcpClient.ps1"

$Token = Get-RovoToken -Token $Token
$cloudId = Resolve-RovoCloudId -SiteUrl $SiteUrl
Write-Host "cloudId: $cloudId"

$sessionId = Connect-RovoSession -Token $Token
Write-Host "Session established: $sessionId"

Invoke-RovoTool -Token $Token -SessionId $sessionId -Name 'search' -Arguments @{
    cloudId   = $cloudId
    query     = $Query
    targetApp = $TargetApp
    limit     = 10
}

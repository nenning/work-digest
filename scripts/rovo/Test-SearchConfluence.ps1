<#
.SYNOPSIS
Verifies a Rovo scoped API token can call the searchConfluence MCP tool (CQL search).

.PARAMETER Token
The scoped API token. If omitted, reads $env:ROVO_API_TOKEN.

.PARAMETER Cql
CQL query string. Defaults to a broad recent-pages query so it works with no arguments.

.PARAMETER SiteUrl
Your Atlassian tenant URL, used only to resolve the cloudId.

.EXAMPLE
$env:ROVO_API_TOKEN = '<token>'
.\Test-SearchConfluence.ps1

.EXAMPLE
.\Test-SearchConfluence.ps1 -Token '<token>' -Cql 'type = page AND space = "DEV" ORDER BY lastmodified DESC'
#>
param(
    [string]$Token,
    [string]$Cql = 'type = page ORDER BY lastmodified DESC',
    [string]$SiteUrl = 'https://yourcompany.atlassian.net'
)

. "$PSScriptRoot\RovoMcpClient.ps1"

$Token = Get-RovoToken -Token $Token
$cloudId = Resolve-RovoCloudId -SiteUrl $SiteUrl
Write-Host "cloudId: $cloudId"

$sessionId = Connect-RovoSession -Token $Token
Write-Host "Session established: $sessionId"

Invoke-RovoTool -Token $Token -SessionId $sessionId -Name 'searchConfluence' -Arguments @{
    cloudId = $cloudId
    cql     = $Cql
    limit   = 5
}

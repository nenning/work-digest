<#
.SYNOPSIS
Verifies a Rovo scoped API token can call the getJiraIssue MCP tool.

.PARAMETER Token
The scoped API token. If omitted, reads $env:ROVO_API_TOKEN.

.PARAMETER IssueKey
Jira issue key to fetch, e.g. PROJ-123.

.PARAMETER SiteUrl
Your Atlassian tenant URL, used only to resolve the cloudId.

.EXAMPLE
$env:ROVO_API_TOKEN = '<token>'
.\Test-GetJiraIssue.ps1 -IssueKey PROJ-123

.EXAMPLE
.\Test-GetJiraIssue.ps1 -Token '<token>' -IssueKey PROJ-123 -SiteUrl 'https://yourcompany.atlassian.net'
#>
param(
    [string]$Token,
    [Parameter(Mandatory)] [string]$IssueKey,
    [string]$SiteUrl = 'https://yourcompany.atlassian.net'
)

. "$PSScriptRoot\RovoMcpClient.ps1"

$Token = Get-RovoToken -Token $Token
$cloudId = Resolve-RovoCloudId -SiteUrl $SiteUrl
Write-Host "cloudId: $cloudId"

$sessionId = Connect-RovoSession -Token $Token
Write-Host "Session established: $sessionId"

Invoke-RovoTool -Token $Token -SessionId $sessionId -Name 'getJiraIssue' -Arguments @{
    cloudId      = $cloudId
    issueIdOrKey = $IssueKey
    view         = 'compact'
}

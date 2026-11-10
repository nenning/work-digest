<#
.SYNOPSIS
Verifies a Rovo scoped API token can call the getTeamworkGraphObject MCP tool
(hydrates one or more objects by ARI or URL).

.PARAMETER Token
The scoped API token. If omitted, reads $env:ROVO_API_TOKEN.

.PARAMETER IssueKey
A Jira issue key to hydrate as a browser URL (simplest smoke test). Ignored if -Objects is given.

.PARAMETER Objects
Explicit list of ARIs or full URLs (max 25), e.g. 'ari:cloud:jira::issue/123' or a browse URL.
Overrides -IssueKey.

.PARAMETER SiteUrl
Your Atlassian tenant URL, used only to resolve the cloudId and to build the default URL.

.EXAMPLE
$env:ROVO_API_TOKEN = '<token>'
.\Test-GetTeamworkGraphObject.ps1 -IssueKey PROJ-123

.EXAMPLE
.\Test-GetTeamworkGraphObject.ps1 -Token '<token>' -Objects @('ari:cloud:jira::issue/10001')
#>
param(
    [string]$Token,
    [string]$IssueKey,
    [string[]]$Objects,
    [string]$SiteUrl = 'https://yourcompany.atlassian.net'
)

. "$PSScriptRoot\RovoMcpClient.ps1"

if (-not $Objects -and -not $IssueKey) {
    throw "Provide either -IssueKey or -Objects."
}
if (-not $Objects) {
    $Objects = @("$($SiteUrl.TrimEnd('/'))/browse/$IssueKey")
}

$Token = Get-RovoToken -Token $Token
$cloudId = Resolve-RovoCloudId -SiteUrl $SiteUrl
Write-Host "cloudId: $cloudId"

$sessionId = Connect-RovoSession -Token $Token
Write-Host "Session established: $sessionId"

Invoke-RovoTool -Token $Token -SessionId $sessionId -Name 'getTeamworkGraphObject' -Arguments @{
    cloudId = $cloudId
    objects = $Objects
}

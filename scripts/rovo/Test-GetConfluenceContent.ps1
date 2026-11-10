<#
.SYNOPSIS
Verifies a Rovo scoped API token can call the getConfluenceContent MCP tool.

.PARAMETER Token
The scoped API token. If omitted, reads $env:ROVO_API_TOKEN.

.PARAMETER ContentId
Numeric Confluence content ID (page/blog/etc). Mutually exclusive with -ContentUrl.

.PARAMETER ContentUrl
Full Confluence content URL (e.g. copied from the browser). Mutually exclusive with -ContentId.

.PARAMETER SiteUrl
Your Atlassian tenant URL, used only to resolve the cloudId.

.EXAMPLE
$env:ROVO_API_TOKEN = '<token>'
.\Test-GetConfluenceContent.ps1 -ContentId 123456

.EXAMPLE
.\Test-GetConfluenceContent.ps1 -Token '<token>' -ContentUrl 'https://yourcompany.atlassian.net/wiki/spaces/DEV/pages/123456/Some+Page'
#>
param(
    [string]$Token,
    [string]$ContentId,
    [string]$ContentUrl,
    [string]$SiteUrl = 'https://yourcompany.atlassian.net'
)

. "$PSScriptRoot\RovoMcpClient.ps1"

if (-not $ContentId -and -not $ContentUrl) {
    throw "Provide either -ContentId or -ContentUrl."
}

$Token = Get-RovoToken -Token $Token
$cloudId = Resolve-RovoCloudId -SiteUrl $SiteUrl
Write-Host "cloudId: $cloudId"

$sessionId = Connect-RovoSession -Token $Token
Write-Host "Session established: $sessionId"

$args = @{ cloudId = $cloudId; detail = 'summary' }
if ($ContentId) { $args['content_id'] = $ContentId }
if ($ContentUrl) { $args['content_url'] = $ContentUrl }

Invoke-RovoTool -Token $Token -SessionId $sessionId -Name 'getConfluenceContent' -Arguments $args

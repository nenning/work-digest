<#
.SYNOPSIS
Verifies a Rovo scoped API token can call the getTeamworkGraphContext MCP tool
(relationship/context lookup for one object).

.PARAMETER Token
The scoped API token. If omitted, reads $env:ROVO_API_TOKEN.

.PARAMETER ObjectType
One of the supported Teamwork Graph object types, e.g. JiraWorkItem, ConfluencePage.
Defaults to JiraWorkItem.

.PARAMETER ObjectIdentifier
Key/ID/ARI/URL identifying the object, e.g. a Jira issue key like PROJ-123.

.PARAMETER SiteUrl
Your Atlassian tenant URL, used only to resolve the cloudId.

.EXAMPLE
$env:ROVO_API_TOKEN = '<token>'
.\Test-GetTeamworkGraphContext.ps1 -ObjectIdentifier PROJ-123

.EXAMPLE
.\Test-GetTeamworkGraphContext.ps1 -Token '<token>' -ObjectType ConfluencePage -ObjectIdentifier 123456
#>
param(
    [string]$Token,
    [ValidateSet(
        'AtlassianGoal', 'AtlassianGoalUpdate', 'AtlassianHomeComment', 'AtlassianHomeTag',
        'AtlassianProject', 'AtlassianProjectUpdate', 'AtlassianTeam', 'AtlassianUser',
        'CompassComponent', 'ConfluenceBlogPost', 'ConfluenceComment', 'ConfluenceDatabase',
        'ConfluencePage', 'ConfluenceSpace', 'ConfluenceWhiteboard', 'ExternalCalendarEvent',
        'ExternalConversation', 'ExternalDeployment', 'ExternalDesign', 'ExternalDocument',
        'ExternalOrganisation', 'ExternalPosition', 'ExternalPullRequest', 'ExternalRepository',
        'ExternalService', 'FocusFocusArea', 'JiraSpace', 'JiraSprint', 'JiraVersion',
        'JiraWorkItem', 'JiraWorkItemComment', 'AssetsObject', 'LoomMeeting',
        'LoomMeetingRecurrence', 'LoomVideo'
    )]
    [string]$ObjectType = 'JiraWorkItem',
    [Parameter(Mandatory)] [string]$ObjectIdentifier,
    [string]$SiteUrl = 'https://yourcompany.atlassian.net'
)

. "$PSScriptRoot\RovoMcpClient.ps1"

$Token = Get-RovoToken -Token $Token
$cloudId = Resolve-RovoCloudId -SiteUrl $SiteUrl
Write-Host "cloudId: $cloudId"

$sessionId = Connect-RovoSession -Token $Token
Write-Host "Session established: $sessionId"

Invoke-RovoTool -Token $Token -SessionId $sessionId -Name 'getTeamworkGraphContext' -Arguments @{
    cloudId          = $cloudId
    objectType       = $ObjectType
    objectIdentifier = $ObjectIdentifier
    detailLevel      = 'summary'
}

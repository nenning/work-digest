# Shared helper functions for testing the Atlassian Rovo MCP server
# (https://mcp.atlassian.com/v2/mcp) with a scoped API token.
#
# Dot-source this file from the Test-*.ps1 scripts in this folder; it is not
# meant to be run directly.

function Invoke-RovoRequest {
    param(
        [Parameter(Mandatory)] [string]$Token,
        [Parameter(Mandatory)] [string]$Body,
        [string]$SessionId,
        [string]$BaseUri = 'https://mcp.atlassian.com/v2/mcp'
    )

    $headers = @{
        'Authorization' = "Bearer $Token"
        'Accept'        = 'application/json, text/event-stream'
    }
    if ($SessionId) { $headers['Mcp-Session-Id'] = $SessionId }

    try {
        return Invoke-WebRequest -Uri $BaseUri -Method Post -Headers $headers `
            -ContentType 'application/json' -Body $Body -UseBasicParsing
    } catch {
        $errResponse = $_.Exception.Response
        if ($errResponse) {
            $stream = $errResponse.GetResponseStream()
            $reader = New-Object System.IO.StreamReader($stream)
            $errBody = $reader.ReadToEnd()
            throw "HTTP $([int]$errResponse.StatusCode) calling Rovo MCP endpoint: $errBody"
        }
        throw
    }
}

function ConvertFrom-RovoSse {
    param([Parameter(Mandatory)] [string]$RawContent)

    $dataLines = $RawContent -split "`n" | Where-Object { $_ -match '^data:\s*' }
    if (-not $dataLines) {
        throw "No SSE 'data:' line found in response body:`n$RawContent"
    }
    $jsonText = ($dataLines -replace '^data:\s*', '') -join ''
    return $jsonText | ConvertFrom-Json
}

function Connect-RovoSession {
    <#
    .SYNOPSIS
    Performs the MCP initialize handshake and returns the session id
    (Mcp-Session-Id header) needed for subsequent tools/call requests.
    #>
    param(
        [Parameter(Mandatory)] [string]$Token,
        [string]$BaseUri = 'https://mcp.atlassian.com/v2/mcp'
    )

    $initBody = @{
        jsonrpc = '2.0'
        id      = 1
        method  = 'initialize'
        params  = @{
            protocolVersion = '2025-06-18'
            capabilities    = @{}
            clientInfo      = @{ name = 'rovo-token-test'; version = '1.0' }
        }
    } | ConvertTo-Json -Depth 10

    $response = Invoke-RovoRequest -Token $Token -Body $initBody -BaseUri $BaseUri

    $sessionId = $response.Headers['Mcp-Session-Id']
    if ($sessionId -is [array]) { $sessionId = $sessionId[0] }
    if (-not $sessionId) {
        throw "Server did not return an Mcp-Session-Id header. Response body: $($response.Content)"
    }

    $parsed = ConvertFrom-RovoSse -RawContent $response.Content
    if ($parsed.error) {
        throw "MCP initialize failed: $($parsed.error | ConvertTo-Json -Depth 10)"
    }

    # Streamable-HTTP transport requires this ack before any other call on the session.
    $ackBody = '{"jsonrpc":"2.0","method":"notifications/initialized"}'
    Invoke-RovoRequest -Token $Token -Body $ackBody -SessionId $sessionId -BaseUri $BaseUri | Out-Null

    return $sessionId
}

function Invoke-RovoTool {
    <#
    .SYNOPSIS
    Calls one MCP tool (tools/call) on an already-initialized session and
    prints/returns the decoded result.
    #>
    param(
        [Parameter(Mandatory)] [string]$Token,
        [Parameter(Mandatory)] [string]$SessionId,
        [Parameter(Mandatory)] [string]$Name,
        [hashtable]$Arguments = @{},
        [string]$BaseUri = 'https://mcp.atlassian.com/v2/mcp'
    )

    $callBody = @{
        jsonrpc = '2.0'
        id      = 2
        method  = 'tools/call'
        params  = @{
            name      = $Name
            arguments = $Arguments
        }
    } | ConvertTo-Json -Depth 20

    $response = Invoke-RovoRequest -Token $Token -Body $callBody -SessionId $SessionId -BaseUri $BaseUri
    $parsed = ConvertFrom-RovoSse -RawContent $response.Content

    if ($parsed.error) {
        throw "MCP error calling '$Name': $($parsed.error | ConvertTo-Json -Depth 10)"
    }

    $result = $parsed.result
    if ($result.isError) {
        Write-Warning "Tool '$Name' returned isError=true"
    }

    foreach ($block in $result.content) {
        if ($block.type -eq 'text') {
            try {
                $block.text | ConvertFrom-Json | ConvertTo-Json -Depth 20
            } catch {
                $block.text
            }
        }
    }
}

function Resolve-RovoCloudId {
    <#
    .SYNOPSIS
    Resolves an Atlassian site URL to its cloudId via the tenant's public,
    unauthenticated _edge/tenant_info endpoint. Note: getAccessibleAtlassianResources
    can return an empty list for some scoped tokens, so this is the reliable fallback.
    #>
    param([string]$SiteUrl = 'https://yourcompany.atlassian.net')

    $uri = "$($SiteUrl.TrimEnd('/'))/_edge/tenant_info"
    $info = Invoke-RestMethod -Uri $uri -Method Get
    return $info.cloudId
}

function Get-RovoToken {
    param([string]$Token)

    if ($Token) { return $Token }
    if ($env:ROVO_API_TOKEN) { return $env:ROVO_API_TOKEN }
    throw "No token supplied. Pass -Token '<token>' or set `$env:ROVO_API_TOKEN first."
}

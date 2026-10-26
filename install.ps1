# Install Forge on Windows: the Databricks base skills and the Databricks MCP server (through AI Dev Kit),
# then the Forge skills in the same skills directories. No clone needed:
#
#   irm https://github.com/bendoan-db/databricks-skill-forge/releases/latest/download/install.ps1 | iex
#   & ([scriptblock]::Create((irm https://github.com/bendoan-db/databricks-skill-forge/releases/latest/download/install.ps1))) -Tools claude,codex
#
# From a checkout, .\install.ps1 installs the checkout's skills. Options: .\install.ps1 -Help.
# Works in Windows PowerShell 5.1 and PowerShell 7. It reads its own arguments (no param block) so it also
# runs through iex, and it never calls exit there, which would close the window. Everything runs inside one
# script block, so no variable or setting is left behind in the caller's session.

& {
$ForgeVersion = ''                        # release tag; tools/build_release.py stamps it (empty in a checkout)
$ForgeSkillsSha256 = ''                   # SHA-256 of that release's forge-skills archive; stamped with it
$ForgeGitHub = 'bendoan-db/databricks-skill-forge'
$AiDevKitPinned = 'v0.2.0'                # the AI Dev Kit release this installer was written against
$AiDevKitPinnedSha256 = 'd27b7c634be3f5080157365de373b9f9261af7de0236f532311b4ac791fa4ce0'  # its GitHub source archive
$MinCliVersion = [version]'1.11.0'        # the Forge skills' own floor; AI Dev Kit checks its per-agent floors
$KnownTools = @('claude', 'codex', 'copilot', 'cursor', 'gemini', 'antigravity', 'opencode', 'kiro', 'windsurf')
$LegacySkills = @('doc-qa-agent', 'structured-doc-extraction', 'unstructured-doc-extraction')  # names before the forge- prefix
$ClaudePlugin = 'databricks@claude-plugins-official'

function Write-Step([string]$Text) { Write-Host ""; Write-Host $Text -ForegroundColor White }
function Write-Msg([string]$Text)  { Write-Host "  $Text" }
function Write-Ok([string]$Text)   { Write-Host "  " -NoNewline; Write-Host "v" -ForegroundColor Green -NoNewline; Write-Host " $Text" }
function Write-Warn([string]$Text) { Write-Host "  " -NoNewline; Write-Host "!" -ForegroundColor Yellow -NoNewline; Write-Host " $Text" }

function Show-Usage {
    @"
Install Forge for your coding agent:
  1. AI Dev Kit ($AiDevKitPinned): every stable Databricks base skill and the MLflow skills (through
     databricks aitools), and the Databricks MCP server, registered with each agent.
  2. Forge: the forge router and its use-case skills, copied next to the base skills, then checked.

Usage: .\install.ps1 [options]

  -Global             Install for your user, in every project, instead of one project
  -Dir DIR            Project to install into (default: the current directory)
  -Tools LIST         Comma-separated agents (default: claude):
                      $($KnownTools -join ',')
  -Profile NAME       Databricks CLI profile for the MCP server (default: DEFAULT)
  -NoMcp              Skip the Databricks MCP server
  -ForgeOnly          Skip AI Dev Kit; install or update only the Forge skills
  -AiDevKitRef REF    Another AI Dev Kit tag or branch (default: $AiDevKitPinned; others aren't checksum-verified)
  -ForgeRef REF       Install the Forge skills from this branch or tag's source, unverified (for development)
  -DryRun             Print the plan and change nothing
  -Yes                Don't ask for confirmation
  -Version            Print the installer's version
  -Help               Show this help
The bash-style spellings (--global, --tools, ...) work too.

Environment:
  FORGE_HOME          Where the AI Dev Kit copy the MCP server runs from lives (default: ~\.databricks-skill-forge)
  FORGE_AIDEVKIT_DIR  Use this AI Dev Kit checkout as is, instead of downloading one
  FORGE_RELEASE_URL, FORGE_ARCHIVE_URL, FORGE_AIDEVKIT_ARCHIVE_URL, FORGE_AIDEVKIT_SHA256
                      Download from a mirror instead of GitHub
"@ | Write-Host
}

function Join-Parts { [IO.Path]::Combine([string[]]$args) }

function Get-Sha256([string]$Path) { (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant() }

# https only (file:// for local mirrors and tests).
function Save-Download([string]$Url, [string]$Dest) {
    if ($Url -like 'file://*') { Copy-Item -LiteralPath ([Uri]$Url).LocalPath -Destination $Dest -Force; return }
    if ($Url -notlike 'https://*') { throw "Refusing to download from ${Url}: use an https:// URL" }
    Invoke-WebRequest -Uri $Url -OutFile $Dest -UseBasicParsing
}

# Windows' own tar.exe when there is one: a Git for Windows tar earlier on PATH reads C:\ paths as remote hosts.
function Get-Tar {
    if ($env:SystemRoot) {
        $windowsTar = Join-Parts $env:SystemRoot 'System32' 'tar.exe'
        if (Test-Path -LiteralPath $windowsTar) { return $windowsTar }
    }
    return 'tar'
}

# Download a .tar.gz, verify it when a checksum is given, and unpack its single top-level folder into Dest.
function Get-Archive([string]$Url, [string]$Dest, [string]$What, [string]$Sha256 = '') {
    $archive = Join-Parts $state.WorkDir 'download.tar.gz'
    try { Save-Download $Url $archive } catch { throw "Couldn't download $What from ${Url}: $($_.Exception.Message)" }
    if ($Sha256) {
        $actual = Get-Sha256 $archive
        if ($actual -ne $Sha256) { throw "The $What archive doesn't match its checksum (expected $Sha256, got $actual). Nothing was installed." }
    }
    if (Test-Path -LiteralPath $Dest) { Remove-Item -LiteralPath $Dest -Recurse -Force }
    New-Item -ItemType Directory -Path $Dest -Force | Out-Null
    & (Get-Tar) -xzf $archive --strip-components 1 -C $Dest
    if ($LASTEXITCODE -ne 0) { throw "Couldn't unpack $What" }
    Remove-Item -LiteralPath $archive -Force
}

# Skills directory for one agent. Same paths as AI Dev Kit's install.ps1, so Forge lands next to the base skills.
function Get-ToolDir([string]$Tool) {
    $userHome = if ($env:USERPROFILE) { $env:USERPROFILE } else { $HOME }
    if ($state.Scope -eq 'global') {
        switch ($Tool) {
            'claude'      { Join-Parts $userHome '.claude' 'skills' }
            'codex'       { Join-Parts $userHome '.agents' 'skills' }
            'copilot'     { Join-Parts $userHome '.github' 'skills' }
            'cursor'      { Join-Parts $userHome '.cursor' 'skills' }
            'gemini'      { Join-Parts $userHome '.gemini' 'skills' }
            'antigravity' { Join-Parts $userHome '.gemini' 'antigravity' 'skills' }
            'opencode'    { Join-Parts $userHome '.config' 'opencode' 'skills' }
            'kiro'        { Join-Parts $userHome '.kiro' 'skills' }
            'windsurf'    { Join-Parts $userHome '.codeium' 'windsurf' 'skills' }
        }
    } else {
        switch ($Tool) {
            'claude'      { Join-Parts $state.BaseDir '.claude' 'skills' }
            'codex'       { Join-Parts $state.BaseDir '.agents' 'skills' }
            'antigravity' { Join-Parts $state.BaseDir '.agents' 'skills' }
            'copilot'     { Join-Parts $state.BaseDir '.github' 'skills' }
            'cursor'      { Join-Parts $state.BaseDir '.cursor' 'skills' }
            'gemini'      { Join-Parts $state.BaseDir '.gemini' 'skills' }
            'opencode'    { Join-Parts $state.BaseDir '.opencode' 'skills' }
            'kiro'        { Join-Parts $state.BaseDir '.kiro' 'skills' }
            'windsurf'    { Join-Parts $state.BaseDir '.windsurf' 'skills' }
        }
    }
}

function Get-SkillDirs { @($state.Tools | ForEach-Object { Get-ToolDir $_ } | Select-Object -Unique) }

function Test-ForgeMade([string]$Dir) {
    $skill = Join-Parts $Dir 'SKILL.md'
    (Test-Path -LiteralPath $skill) -and (Select-String -LiteralPath $skill -Pattern '^  forge_kind:' -Quiet)
}

# The PowerShell that's running this script, so AI Dev Kit's installers run in the same edition. Their
# output goes to the console, so only the exit code comes back.
function Invoke-PowerShellFile([string]$File, [string[]]$Arguments) {
    $exe = (Get-Process -Id $PID).Path
    & $exe -NoProfile -ExecutionPolicy Bypass -File $File @Arguments | Out-Host
    return $LASTEXITCODE
}

# Run a native command whose stderr is redirected. Windows PowerShell 5.1 turns redirected stderr lines
# into errors, which 'Stop' would make fatal.
function Invoke-Native([scriptblock]$Command) {
    $savedPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $Command } finally { $ErrorActionPreference = $savedPreference }
}

function Install-Forge([string[]]$Arguments) {
    $dbProfile = if ($env:DATABRICKS_CONFIG_PROFILE) { $env:DATABRICKS_CONFIG_PROFILE } else { 'DEFAULT' }
    $installMcp = $true; $forgeOnly = $false; $forgeRef = ''; $dryRun = $false; $assumeYes = $false
    $aiDevKitRef = $AiDevKitPinned

    $i = 0
    while ($i -lt $Arguments.Count) {
        $arg = $Arguments[$i]
        $value = if ($i + 1 -lt $Arguments.Count) { $Arguments[$i + 1] } else { $null }
        switch -Regex ($arg) {
            '^(-g|--global|-Global)$'                { $state.Scope = 'global'; $i++; continue }
            '^(-d|--dir|-Dir)$'                      { if ($null -eq $value) { throw "$arg needs a value" }; $state.BaseDir = $value; $i += 2; continue }
            '^(-t|--tools|-Tools)$'                  { if ($null -eq $value) { throw "$arg needs a value" }; $state.Tools = @("$value" -split '[,\s]+' | Where-Object { $_ }); $i += 2; continue }
            '^(-p|--profile|-Profile)$'              { if ($null -eq $value) { throw "$arg needs a value" }; $dbProfile = $value; $i += 2; continue }
            '^(--no-mcp|-NoMcp)$'                    { $installMcp = $false; $i++; continue }
            '^(--forge-only|-ForgeOnly)$'            { $forgeOnly = $true; $i++; continue }
            '^(--ai-dev-kit-ref|-AiDevKitRef)$'      { if ($null -eq $value) { throw "$arg needs a value" }; $aiDevKitRef = $value; $i += 2; continue }
            '^(--forge-ref|-ForgeRef)$'              { if ($null -eq $value) { throw "$arg needs a value" }; $forgeRef = $value; $i += 2; continue }
            '^(--dry-run|-DryRun)$'                  { $dryRun = $true; $i++; continue }
            '^(-y|--yes|-Yes)$'                      { $assumeYes = $true; $i++; continue }
            '^(-v|--version|-Version)$'              { $v = if ($ForgeVersion) { $ForgeVersion } else { '(development checkout)' }; Write-Host "Forge installer $v, AI Dev Kit $aiDevKitRef"; return }
            '^(-h|--help|-Help|-\?)$'                { Show-Usage; return }
            default                                  { throw "Unknown option: $arg (see -Help)" }
        }
    }
    foreach ($tool in $state.Tools) {
        if ($KnownTools -notcontains $tool) { throw "Unknown tool '$tool'. Use: $($KnownTools -join ',')" }
    }
    if ($state.Tools.Count -eq 0) { throw "-Tools is empty" }
    $toolsCsv = $state.Tools -join ','
    if ($state.Scope -eq 'global') {
        $state.BaseDir = if ($env:USERPROFILE) { $env:USERPROFILE } else { $HOME }
    } else {
        if (-not (Test-Path -LiteralPath $state.BaseDir -PathType Container)) { throw "Project directory not found: $($state.BaseDir)" }
        $state.BaseDir = (Resolve-Path -LiteralPath $state.BaseDir).Path
    }
    $forgeHome = if ($env:FORGE_HOME) { $env:FORGE_HOME } else { Join-Parts $(if ($env:USERPROFILE) { $env:USERPROFILE } else { $HOME }) '.databricks-skill-forge' }
    $releaseUrl = if ($env:FORGE_RELEASE_URL) { $env:FORGE_RELEASE_URL } else { "https://github.com/$ForgeGitHub/releases/download" }
    $archiveUrl = if ($env:FORGE_ARCHIVE_URL) { $env:FORGE_ARCHIVE_URL } else { "https://codeload.github.com/$ForgeGitHub/tar.gz" }
    $aiDevKitUrl = if ($env:FORGE_AIDEVKIT_ARCHIVE_URL) { $env:FORGE_AIDEVKIT_ARCHIVE_URL } else { 'https://codeload.github.com/databricks-solutions/ai-dev-kit/tar.gz' }
    # Only the pinned release has a known checksum (a mirror can supply its own).
    $aiDevKitSha256 = if ($env:FORGE_AIDEVKIT_SHA256) { $env:FORGE_AIDEVKIT_SHA256 } elseif ($aiDevKitRef -eq $AiDevKitPinned) { $AiDevKitPinnedSha256 } else { '' }

    # --- Prerequisites ----------------------------------------------------------
    Write-Step 'Checking prerequisites'
    if (-not (Get-Command databricks -ErrorAction SilentlyContinue)) { throw "The Databricks CLI isn't installed: https://docs.databricks.com/aws/en/dev-tools/cli/install" }
    $cliText = (Invoke-Native { & databricks --version 2>$null }) -join ' '
    if ($cliText -match '(\d+\.\d+\.\d+)') { $cliVersion = [version]$Matches[1] }
    else { throw "Couldn't read the Databricks CLI version from 'databricks --version'" }
    if ($cliVersion -lt $MinCliVersion) { throw "Databricks CLI $cliVersion is too old; Forge needs v$MinCliVersion or later" }
    Write-Ok "Databricks CLI $cliVersion"
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        Write-Ok "uv $(((& uv --version) -split ' ')[1])"
    } elseif ($dryRun) {
        Write-Warn "uv isn't installed; the install needs it (https://docs.astral.sh/uv/)"
    } else {
        throw "uv isn't installed; Forge's scripts and the MCP server need it: https://docs.astral.sh/uv/"
    }
    if (-not (Get-Command (Get-Tar) -ErrorAction SilentlyContinue)) { throw "tar isn't available; it ships with Windows 10 (1803) and later" }
    $cfg = if ($env:DATABRICKS_CONFIG_FILE) { $env:DATABRICKS_CONFIG_FILE } else { Join-Parts $(if ($env:USERPROFILE) { $env:USERPROFILE } else { $HOME }) '.databrickscfg' }
    if ((Test-Path -LiteralPath $cfg) -and (Select-String -LiteralPath $cfg -Pattern "^\[$([regex]::Escape($dbProfile))\]" -Quiet)) {
        Write-Ok "Databricks profile $dbProfile"
    } else {
        Write-Warn "Profile '$dbProfile' isn't in $cfg yet. Create it later with: databricks auth login --profile $dbProfile"
    }

    # --- Forge skills: a release, this checkout, or a source archive -------------
    # Downloads are verified (when they can be) before anything else happens.
    if ($forgeRef) {
        Get-Archive "$archiveUrl/$forgeRef" (Join-Parts $state.WorkDir 'forge-source') "Forge $forgeRef source"
        $forgeSkills = Join-Parts $state.WorkDir 'forge-source' 'skills'
        $forgeSource = "the $forgeRef source, unverified (-ForgeRef)"
    } elseif ($ForgeVersion) {
        Get-Archive "$releaseUrl/$ForgeVersion/forge-skills-$ForgeVersion.tar.gz" (Join-Parts $state.WorkDir 'forge-skills') "Forge $ForgeVersion skills" $ForgeSkillsSha256
        $forgeSkills = Join-Parts $state.WorkDir 'forge-skills'
        $forgeSource = "release $ForgeVersion, checksum verified"
    } elseif ($PSScriptRoot -and (Test-Path -LiteralPath (Join-Parts $PSScriptRoot 'skills' 'forge' 'SKILL.md'))) {
        $forgeSkills = Join-Parts $PSScriptRoot 'skills'
        $forgeSource = "the checkout in $PSScriptRoot"
    } else {
        Write-Warn "This installer isn't from a release, so it installs the main branch, unverified. For a release:"
        Write-Warn "irm https://github.com/$ForgeGitHub/releases/latest/download/install.ps1 | iex"
        Get-Archive "$archiveUrl/main" (Join-Parts $state.WorkDir 'forge-source') 'Forge main source'
        $forgeSkills = Join-Parts $state.WorkDir 'forge-source' 'skills'
        $forgeSource = 'the main source, unverified'
    }
    $forgeNames = @(Get-ChildItem -LiteralPath $forgeSkills -Directory | Where-Object { Test-Path -LiteralPath (Join-Parts $_.FullName 'SKILL.md') } | ForEach-Object Name | Sort-Object)
    $useCaseNames = @($forgeNames | Where-Object { Test-Path -LiteralPath (Join-Parts $forgeSkills $_ 'ucs.yaml') })
    if ($useCaseNames.Count -eq 0) { throw "No Forge skills found in $forgeSkills" }

    # Base skills the use-case skills load: every `- name:` under a module's base_skills in ucs.yaml.
    $requiredBase = @()
    foreach ($name in $useCaseNames) {
        $on = $false
        foreach ($line in Get-Content -LiteralPath (Join-Parts $forgeSkills $name 'ucs.yaml')) {
            if ($line -match '^    base_skills:') { $on = $true; continue }
            if ($line -match '^[a-z_]+:' -or $line -match '^  - id:' -or $line -match '^    [a-z_]+:') { $on = $false }
            if ($on -and $line -match '- name:\s*(\S+)') { $requiredBase += $Matches[1] }
        }
    }
    $requiredBase = @($requiredBase | Sort-Object -Unique)

    # --- Base skills to request from AI Dev Kit ---------------------------------
    # Named, not the "all" profile: for Claude Code, Codex, and Copilot, "all" installs a plugin, and Forge
    # needs the base skills as folders in the same skills directory.
    $stableBase = @(); $baseRelease = ''
    if (-not $forgeOnly) {
        $listing = $null
        try { $listing = ((Invoke-Native { & databricks aitools list -o json 2>$null }) -join "`n") | ConvertFrom-Json } catch { $listing = $null }
        if ($listing -and $listing.skills) {
            $stableBase = @($listing.skills | Where-Object { -not $_.experimental } | ForEach-Object { $_.name })
            $baseRelease = "$($listing.release)"
        }
        if ($stableBase.Count -eq 0) { throw "'databricks aitools list -o json' returned no skills. Check the CLI and your network, then rerun." }
        $missing = @($requiredBase | Where-Object { $stableBase -notcontains $_ })
        if ($missing.Count -gt 0) { throw "Databricks agent skills $baseRelease lack the base skills Forge needs: $($missing -join ' '). Upgrade the Databricks CLI." }
    }

    # The MCP server runs from a persistent AI Dev Kit copy; a skills-only install uses a throwaway one.
    $aidkDir = if ($env:FORGE_AIDEVKIT_DIR) { $env:FORGE_AIDEVKIT_DIR } elseif ($installMcp) { Join-Parts $forgeHome 'ai-dev-kit' } else { Join-Parts $state.WorkDir 'ai-dev-kit' }

    # --- Plan --------------------------------------------------------------------
    Write-Step 'Plan'
    Write-Msg "Scope:        $($state.Scope) ($($state.BaseDir))"
    Write-Msg "Agents:       $toolsCsv"
    Write-Msg "Skills dirs:  $((Get-SkillDirs) -join ' ')"
    if ($forgeOnly) {
        Write-Msg 'AI Dev Kit:   skipped (-ForgeOnly); the base skills must already be installed'
    } else {
        if ($env:FORGE_AIDEVKIT_DIR) { Write-Msg "AI Dev Kit:   the copy in $aidkDir" }
        elseif ($aiDevKitSha256) { Write-Msg "AI Dev Kit:   $aiDevKitRef, checksum pinned, in $aidkDir" }
        else { Write-Msg "AI Dev Kit:   $aiDevKitRef, unverified (only $AiDevKitPinned has a pinned checksum), in $aidkDir" }
        $releaseText = if ($baseRelease) { "$baseRelease " } else { '' }
        Write-Msg "Base skills:  $($stableBase.Count) stable skills from databricks-agent-skills ${releaseText}as folders, plus the MLflow skills"
        if ($installMcp) { Write-Msg "MCP server:   Databricks MCP server for $toolsCsv, profile $dbProfile" } else { Write-Msg 'MCP server:   skipped (-NoMcp)' }
    }
    Write-Msg "Forge:        $($forgeNames -join ' ') from $forgeSource (replaces existing copies)"
    $legacyFound = @()
    foreach ($dir in Get-SkillDirs) {
        foreach ($name in $LegacySkills) { if (Test-ForgeMade (Join-Parts $dir $name)) { $legacyFound += (Join-Parts $dir $name) } }
    }
    if ($legacyFound.Count -gt 0) { Write-Msg "Removes:      Forge skills installed before the forge- prefix: $($legacyFound -join ' ')" }
    if (($state.Tools -contains 'claude') -and (Get-Command claude -ErrorAction SilentlyContinue)) {
        $plugins = (Invoke-Native { & claude plugin list 2>$null }) -join "`n"
        if ($plugins -like "*$ClaudePlugin*") {
            Write-Warn "The Databricks Claude Code plugin ($ClaudePlugin) is installed, so Claude Code will see the base skills twice."
            Write-Warn "Forge uses the folders. To remove the plugin copy: claude plugin uninstall $ClaudePlugin"
        }
    }

    if ($dryRun) { Write-Host ""; Write-Host 'Dry run: nothing was changed.'; return }
    if (-not $assumeYes) {
        if ([Console]::IsInputRedirected) { throw 'No terminal to confirm on. Rerun with -Yes to install without confirming.' }
        $answer = Read-Host "`nProceed? [y/N]"
        if ($answer -notmatch '^(y|yes)$') { Write-Msg 'Cancelled; nothing was changed.'; return }
    }

    # Assigned, not returned from an if: an empty @() result would add an empty argument.
    $scopeArgs = @(); $mcpScopeArgs = @()
    if ($state.Scope -eq 'global') { $scopeArgs = @('--global'); $mcpScopeArgs = @('-Global') }

    # --- 1. AI Dev Kit: base skills and MCP server -------------------------------
    if (-not $forgeOnly) {
        Write-Step "AI Dev Kit $aiDevKitRef"
        $previous = ''
        $stamp = "$aiDevKitRef $(if ($aiDevKitSha256) { $aiDevKitSha256 } else { 'unverified' })"
        $stampFile = Join-Parts $aidkDir '.forge-source'
        if ($env:FORGE_AIDEVKIT_DIR) {
            if (-not (Test-Path -LiteralPath (Join-Parts $aidkDir 'install.ps1'))) { throw "FORGE_AIDEVKIT_DIR=$aidkDir has no install.ps1" }
            Write-Ok "Using the copy in $aidkDir"
        } elseif ($aiDevKitSha256 -and (Test-Path -LiteralPath $stampFile) -and ((Get-Content -LiteralPath $stampFile -Raw).Trim() -eq $stamp)) {
            Write-Ok "Already have $aiDevKitRef in $aidkDir"
        } else {
            # Unpack next to the current copy, then swap, so a failed MCP install can put the old one back.
            Get-Archive "$aiDevKitUrl/$aiDevKitRef" "$aidkDir.new" "AI Dev Kit $aiDevKitRef" $aiDevKitSha256
            if (-not (Test-Path -LiteralPath (Join-Parts "$aidkDir.new" 'install.ps1'))) { throw "The AI Dev Kit $aiDevKitRef archive has no install.ps1" }
            Set-Content -LiteralPath (Join-Parts "$aidkDir.new" '.forge-source') -Value $stamp
            if (Test-Path -LiteralPath $aidkDir) {
                if (Test-Path -LiteralPath "$aidkDir.old") { Remove-Item -LiteralPath "$aidkDir.old" -Recurse -Force }
                Move-Item -LiteralPath $aidkDir -Destination "$aidkDir.old"
                $previous = "$aidkDir.old"
            }
            Move-Item -LiteralPath "$aidkDir.new" -Destination $aidkDir
            Write-Ok "Downloaded AI Dev Kit $aiDevKitRef$(if ($aiDevKitSha256) { ' (checksum verified)' })"
        }
        $restorePrevious = {
            if ($previous) { Remove-Item -LiteralPath $aidkDir -Recurse -Force; Move-Item -LiteralPath $previous -Destination $aidkDir }
        }

        $mlflowSkills = @()
        $installer = Get-Content -LiteralPath (Join-Parts $aidkDir 'install.ps1') -Raw
        if ($installer -match '(?s)\$script:MlflowSkills = @\((.*?)\)') { $mlflowSkills = @([regex]::Matches($Matches[1], '"([^"]+)"') | ForEach-Object { $_.Groups[1].Value }) }
        if ($mlflowSkills.Count -eq 0) { Write-Warn "Couldn't read the MLflow skill list from AI Dev Kit; installing the Databricks skills only" }
        $skillsCsv = (@($stableBase) + @($mlflowSkills)) -join ','

        Write-Msg "Installing $((@($stableBase) + @($mlflowSkills)).Count) skills for $toolsCsv (through databricks aitools)..."
        # --silent skips AI Dev Kit's own prompts (this script already confirmed); --force reapplies the named set.
        $env:DEVKIT_SCOPE = $state.Scope  # restored when the script ends
        Push-Location -LiteralPath $state.BaseDir
        try {
            $code = Invoke-PowerShellFile (Join-Parts $aidkDir 'install.ps1') (@('--tools', $toolsCsv, '-p', $dbProfile, '--skills', $skillsCsv, '--experimental', 'false', '--force', '--silent') + $scopeArgs)
        } finally { Pop-Location }
        if ($code -ne 0) { & $restorePrevious; throw "AI Dev Kit couldn't install the base skills (see its output above)" }
        Write-Ok 'Base skills installed; manage them with: databricks aitools list|update'

        if ($installMcp) {
            Write-Msg 'Registering the Databricks MCP server...'
            Push-Location -LiteralPath $state.BaseDir
            try {
                $code = Invoke-PowerShellFile (Join-Parts $aidkDir 'databricks-mcp-server' 'mcp_install.ps1') (@('-Tools', $toolsCsv, '-Profile_', $dbProfile, '-Silent') + $mcpScopeArgs)
            } finally { Pop-Location }
            if ($code -eq 0) {
                Write-Ok "Databricks MCP server registered for $toolsCsv (it runs from $aidkDir)"
                if ($previous) { Remove-Item -LiteralPath $previous -Recurse -Force }
            } elseif ($previous) {
                & $restorePrevious
                Write-Warn "The MCP server didn't install, so the previous AI Dev Kit copy it ran from was put back."
            } else {
                Write-Warn "The MCP server didn't install. The skills don't need it; retry with: $(Join-Parts $aidkDir 'databricks-mcp-server' 'mcp_install.ps1')"
            }
        }
    }

    # --- 2. Forge skills, next to the base skills --------------------------------
    Write-Step 'Forge skills'
    $installed = @()
    foreach ($dir in Get-SkillDirs) {
        if (-not (Test-Path -LiteralPath (Join-Parts $dir 'databricks-core' 'SKILL.md'))) {
            Write-Warn "No base skills in $dir; skipped. Install them first (rerun without -ForgeOnly)."
            continue
        }
        foreach ($name in $forgeNames) {
            $target = Join-Parts $dir $name
            if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Recurse -Force }
            Copy-Item -LiteralPath (Join-Parts $forgeSkills $name) -Destination $target -Recurse
            Get-ChildItem -LiteralPath $target -Recurse -Directory -Force |
                Where-Object { $_.Name -in @('__pycache__', '.ruff_cache', '.pytest_cache') } |
                Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
        }
        foreach ($name in $LegacySkills) {
            $legacy = Join-Parts $dir $name
            if (Test-ForgeMade $legacy) { Remove-Item -LiteralPath $legacy -Recurse -Force; Write-Ok "Removed the pre-prefix copy $legacy" }
        }
        Write-Ok "Forge skills -> $dir"
        $installed += $dir
    }
    if ($installed.Count -eq 0) { throw "No skills directory had the base skills, so Forge wasn't installed." }

    # --- Check: each use-case skill finds its base skills ------------------------
    Write-Step 'Checking'
    $failed = $false
    foreach ($dir in $installed) {
        foreach ($name in $useCaseNames) {
            $errors = Join-Parts $state.WorkDir 'check.err'
            Invoke-Native { & uv run -q (Join-Parts $dir $name 'scripts' 'check_base_skills.py') --skills-dir $dir 2> $errors | Out-Null }
            if ($LASTEXITCODE -eq 0) { Write-Ok "${name}: base skills present ($dir)" }
            else { $failed = $true; Write-Warn "${name}: $((Get-Content -LiteralPath $errors | Select-Object -Last 1))" }
        }
    }
    if ($failed) { throw 'Some use-case skills are missing base skills; see the warnings above.' }

    Write-Step 'Done'
    Write-Msg 'Restart your agent so it loads the new skills, then type /forge and say what you want to build.'
}

$state = @{
    Scope = 'project'
    BaseDir = (Get-Location).Path
    Tools = @('claude')
    WorkDir = Join-Path ([IO.Path]::GetTempPath()) ("forge-install-" + [Guid]::NewGuid().ToString('N'))
}
New-Item -ItemType Directory -Path $state.WorkDir -Force | Out-Null
$previousScope = $env:DEVKIT_SCOPE
$failure = $null
try {
    $ErrorActionPreference = 'Stop'
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    Install-Forge -Arguments ([string[]]@($args | ForEach-Object { "$_" }))
} catch {
    $failure = $_.Exception.Message
} finally {
    Remove-Item -LiteralPath $state.WorkDir -Recurse -Force -ErrorAction SilentlyContinue
    if ($null -eq $previousScope) { Remove-Item Env:DEVKIT_SCOPE -ErrorAction SilentlyContinue } else { $env:DEVKIT_SCOPE = $previousScope }
}
if ($failure) {
    Write-Host "Error: $failure" -ForegroundColor Red
    # exit only when run as a file: in `irm | iex` it would close the user's PowerShell window.
    if ($PSCommandPath) { exit 1 }
}
} @args

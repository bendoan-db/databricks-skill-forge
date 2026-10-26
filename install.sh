#!/usr/bin/env bash
# Install Forge: the Databricks base skills and the Databricks MCP server (through AI Dev Kit), then the
# Forge skills in the same skills directories. No clone needed:
#
#   bash <(curl -fsSL https://github.com/bendoan-db/databricks-skill-forge/releases/latest/download/install.sh)
#
# From a checkout, ./install.sh installs the checkout's skills. Options: ./install.sh --help.
# Runs on macOS's bash 3.2.
set -euo pipefail

FORGE_VERSION=""                          # release tag; tools/build_release.py stamps it (empty in a checkout)
FORGE_SKILLS_SHA256=""                    # SHA-256 of that release's forge-skills archive; stamped with it
FORGE_GITHUB="bendoan-db/databricks-skill-forge"
FORGE_RELEASE_URL="${FORGE_RELEASE_URL:-https://github.com/$FORGE_GITHUB/releases/download}"
FORGE_ARCHIVE_URL="${FORGE_ARCHIVE_URL:-https://codeload.github.com/$FORGE_GITHUB/tar.gz}"
AIDEVKIT_REF="v0.2.0"                     # the AI Dev Kit release this installer was written against
AIDEVKIT_SHA256="d27b7c634be3f5080157365de373b9f9261af7de0236f532311b4ac791fa4ce0"  # its GitHub source archive
AIDEVKIT_ARCHIVE_URL="${FORGE_AIDEVKIT_ARCHIVE_URL:-https://codeload.github.com/databricks-solutions/ai-dev-kit/tar.gz}"
FORGE_HOME="${FORGE_HOME:-$HOME/.databricks-skill-forge}"
MIN_CLI_VERSION="1.11.0"                  # the Forge skills' own floor; AI Dev Kit checks its per-agent floors
KNOWN_TOOLS="claude codex copilot cursor gemini antigravity opencode kiro windsurf"
LEGACY_SKILLS="doc-qa-agent structured-doc-extraction unstructured-doc-extraction"  # names before the forge- prefix
CLAUDE_PLUGIN="databricks@claude-plugins-official"

SCOPE="project"
BASE_DIR="$(pwd)"
TOOLS="claude"
PROFILE="${DATABRICKS_CONFIG_PROFILE:-DEFAULT}"
INSTALL_MCP=true
FORGE_ONLY=false
FORGE_REF=""
DRY_RUN=false
ASSUME_YES=false
AIDEVKIT_PINNED="$AIDEVKIT_REF"

if [ -t 1 ]; then B='\033[1m'; G='\033[32m'; Y='\033[33m'; R='\033[31m'; N='\033[0m'; else B=''; G=''; Y=''; R=''; N=''; fi
step() { printf '\n%b%s%b\n' "$B" "$*" "$N"; }
msg()  { printf '  %s\n' "$*"; }
ok()   { printf '  %b✓%b %s\n' "$G" "$N" "$*"; }
warn() { printf '  %b!%b %s\n' "$Y" "$N" "$*" >&2; }
die()  { printf '%bError:%b %s\n' "$R" "$N" "$*" >&2; exit 1; }

usage() {
    cat <<EOF
Install Forge for your coding agent:
  1. AI Dev Kit ($AIDEVKIT_REF): every stable Databricks base skill and the MLflow skills (through
     databricks aitools), and the Databricks MCP server, registered with each agent.
  2. Forge: the forge router and its use-case skills, copied next to the base skills, then checked.

Usage: install.sh [options]

  -g, --global             Install for your user, in every project, instead of one project
  -d, --dir DIR            Project to install into (default: the current directory)
  -t, --tools LIST         Comma-separated agents (default: claude):
                           ${KNOWN_TOOLS// /,}
  -p, --profile NAME       Databricks CLI profile for the MCP server (default: DEFAULT)
      --no-mcp             Skip the Databricks MCP server
      --forge-only         Skip AI Dev Kit; install or update only the Forge skills
      --ai-dev-kit-ref REF Another AI Dev Kit tag or branch (default: $AIDEVKIT_REF; others aren't checksum-verified)
      --forge-ref REF      Install the Forge skills from this branch or tag's source, unverified (for development)
      --dry-run            Print the plan and change nothing
  -y, --yes                Don't ask for confirmation
  -v, --version            Print the installer's version
  -h, --help               Show this help

Environment:
  FORGE_HOME          Where the AI Dev Kit copy the MCP server runs from lives (default: ~/.databricks-skill-forge)
  FORGE_AIDEVKIT_DIR  Use this AI Dev Kit checkout as is, instead of downloading one
  FORGE_RELEASE_URL, FORGE_ARCHIVE_URL, FORGE_AIDEVKIT_ARCHIVE_URL, FORGE_AIDEVKIT_SHA256
                      Download from a mirror instead of GitHub
EOF
}

while [ $# -gt 0 ]; do
    case $1 in
        -g|--global)      SCOPE="global"; shift ;;
        -d|--dir)         [ $# -ge 2 ] || die "$1 needs a value"; BASE_DIR="$2"; shift 2 ;;
        -t|--tools)       [ $# -ge 2 ] || die "$1 needs a value"; TOOLS="$(echo "$2" | tr ',' ' ')"; shift 2 ;;
        -p|--profile)     [ $# -ge 2 ] || die "$1 needs a value"; PROFILE="$2"; shift 2 ;;
        --no-mcp)         INSTALL_MCP=false; shift ;;
        --forge-only)     FORGE_ONLY=true; shift ;;
        --ai-dev-kit-ref) [ $# -ge 2 ] || die "$1 needs a value"; AIDEVKIT_REF="$2"; shift 2 ;;
        --forge-ref)      [ $# -ge 2 ] || die "$1 needs a value"; FORGE_REF="$2"; shift 2 ;;
        --dry-run)        DRY_RUN=true; shift ;;
        -y|--yes)         ASSUME_YES=true; shift ;;
        -v|--version)     echo "Forge installer ${FORGE_VERSION:-(development checkout)}, AI Dev Kit $AIDEVKIT_REF"; exit 0 ;;
        -h|--help)        usage; exit 0 ;;
        *)                die "Unknown option: $1 (see --help)" ;;
    esac
done

for tool in $TOOLS; do
    case " $KNOWN_TOOLS " in *" $tool "*) ;; *) die "Unknown tool '$tool'. Use: ${KNOWN_TOOLS// /,}" ;; esac
done
[ -n "$TOOLS" ] || die "--tools is empty"
TOOLS_CSV="$(echo "$TOOLS" | tr ' ' ',')"
if [ "$SCOPE" = "global" ]; then
    BASE_DIR="$HOME"
else
    [ -d "$BASE_DIR" ] || die "Project directory not found: $BASE_DIR"
    BASE_DIR="$(cd "$BASE_DIR" && pwd)"
fi
# Only the pinned release has a known checksum (a mirror can supply its own).
if [ -n "${FORGE_AIDEVKIT_SHA256:-}" ]; then
    AIDEVKIT_SHA256="$FORGE_AIDEVKIT_SHA256"
elif [ "$AIDEVKIT_REF" != "$AIDEVKIT_PINNED" ]; then
    AIDEVKIT_SHA256=""
fi

# version_gte 1.20.0 1.11.0
version_gte() {
    local IFS=. i
    local -a have want
    read -r -a have <<< "$1"
    read -r -a want <<< "$2"
    for i in 0 1 2; do
        [ "${have[$i]:-0}" -gt "${want[$i]:-0}" ] && return 0
        [ "${have[$i]:-0}" -lt "${want[$i]:-0}" ] && return 1
    done
    return 0
}

# download URL DEST: https only (file:// for local mirrors and tests); fails on any HTTP error.
download() {
    case $1 in
        https://*) curl -fsSL --proto '=https' --tlsv1.2 --retry 2 -o "$2" "$1" ;;
        file://*)  curl -fsSL -o "$2" "$1" ;;
        *)         die "Refusing to download from $1: use an https:// URL" ;;
    esac
}

sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'; else shasum -a 256 "$1" | awk '{print $1}'; fi
}

# verify FILE EXPECTED WHAT: stop before anything is installed when a download doesn't match.
verify() {
    local actual
    actual="$(sha256_of "$1")"
    [ "$actual" = "$2" ] || die "The $3 archive doesn't match its checksum (expected $2, got $actual). Nothing was installed."
}

# fetch_archive URL DEST_DIR WHAT [SHA256]: download a .tar.gz, verify it when a checksum is given, unpack
# its single top-level folder into DEST_DIR.
fetch_archive() {
    local archive="$WORK_DIR/download.tar.gz"
    download "$1" "$archive" || die "Couldn't download $3 from $1"
    [ -z "${4:-}" ] || verify "$archive" "$4" "$3"
    rm -rf "$2" && mkdir -p "$2"
    tar -xzf "$archive" --strip-components 1 -C "$2" || die "Couldn't unpack $3"
    rm -f "$archive"
}

# Skills directory for one agent. Same paths as AI Dev Kit, so Forge lands next to the base skills.
tool_dir() {
    if [ "$SCOPE" = "global" ]; then
        case $1 in
            claude)      echo "$HOME/.claude/skills" ;;
            codex)       echo "$HOME/.agents/skills" ;;
            copilot)     echo "$HOME/.github/skills" ;;
            cursor)      echo "$HOME/.cursor/skills" ;;
            gemini)      local root="${GEMINI_CLI_HOME:-$HOME}"; echo "${root%/}/.gemini/skills" ;;
            antigravity) echo "$HOME/.gemini/antigravity/skills" ;;
            opencode)    echo "$HOME/.config/opencode/skills" ;;
            kiro)        echo "$HOME/.kiro/skills" ;;
            windsurf)    echo "$HOME/.codeium/windsurf/skills" ;;
        esac
    else
        case $1 in
            claude)            echo "$BASE_DIR/.claude/skills" ;;
            codex|antigravity) echo "$BASE_DIR/.agents/skills" ;;
            copilot)           echo "$BASE_DIR/.github/skills" ;;
            cursor)            echo "$BASE_DIR/.cursor/skills" ;;
            gemini)            echo "$BASE_DIR/.gemini/skills" ;;
            opencode)          echo "$BASE_DIR/.opencode/skills" ;;
            kiro)              echo "$BASE_DIR/.kiro/skills" ;;
            windsurf)          echo "$BASE_DIR/.windsurf/skills" ;;
        esac
    fi
}

skill_dirs() {
    local tool
    for tool in $TOOLS; do tool_dir "$tool"; done | awk '!seen[$0]++'
}

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

# ─── Prerequisites ──────────────────────────────────────────────────────────────
step "Checking prerequisites"
command -v databricks >/dev/null 2>&1 || die "The Databricks CLI isn't installed: https://docs.databricks.com/aws/en/dev-tools/cli/install"
CLI_VERSION="$(databricks --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)"
[ -n "$CLI_VERSION" ] || die "Couldn't read the Databricks CLI version from 'databricks --version'"
version_gte "$CLI_VERSION" "$MIN_CLI_VERSION" || die "Databricks CLI $CLI_VERSION is too old; Forge needs v$MIN_CLI_VERSION or later"
ok "Databricks CLI $CLI_VERSION"
if command -v uv >/dev/null 2>&1; then
    ok "uv $(uv --version 2>/dev/null | awk '{print $2}')"
elif [ "$DRY_RUN" = true ]; then
    warn "uv isn't installed; the install needs it (https://docs.astral.sh/uv/)"
else
    die "uv isn't installed; Forge's scripts and the MCP server need it: https://docs.astral.sh/uv/"
fi
for tool in curl tar; do
    command -v "$tool" >/dev/null 2>&1 || die "$tool isn't installed; it's needed to download and unpack the skills"
done
command -v sha256sum >/dev/null 2>&1 || command -v shasum >/dev/null 2>&1 || die "Neither sha256sum nor shasum is installed; one is needed to verify downloads"
cfg="${DATABRICKS_CONFIG_FILE:-$HOME/.databrickscfg}"
if [ -f "$cfg" ] && grep -q "^\[$PROFILE\]" "$cfg"; then
    ok "Databricks profile $PROFILE"
else
    warn "Profile '$PROFILE' isn't in $cfg yet. Create it later with: databricks auth login --profile $PROFILE"
fi

# ─── Forge skills: a release, this checkout, or a source archive ─────────────────
# Downloads are verified (when they can be) before anything else happens.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || true)"
if [ -n "$FORGE_REF" ]; then
    fetch_archive "$FORGE_ARCHIVE_URL/$FORGE_REF" "$WORK_DIR/forge-source" "Forge $FORGE_REF source"
    FORGE_SKILLS="$WORK_DIR/forge-source/skills"
    FORGE_SOURCE="the $FORGE_REF source, unverified (--forge-ref)"
elif [ -n "$FORGE_VERSION" ]; then
    fetch_archive "$FORGE_RELEASE_URL/$FORGE_VERSION/forge-skills-$FORGE_VERSION.tar.gz" "$WORK_DIR/forge-skills" \
        "Forge $FORGE_VERSION skills" "$FORGE_SKILLS_SHA256"
    FORGE_SKILLS="$WORK_DIR/forge-skills"
    FORGE_SOURCE="release $FORGE_VERSION, checksum verified"
elif [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/skills/forge/SKILL.md" ]; then
    FORGE_SKILLS="$SCRIPT_DIR/skills"
    FORGE_SOURCE="the checkout in $SCRIPT_DIR"
else
    warn "This installer isn't from a release, so it installs the main branch, unverified. For a release:"
    warn "bash <(curl -fsSL https://github.com/$FORGE_GITHUB/releases/latest/download/install.sh)"
    fetch_archive "$FORGE_ARCHIVE_URL/main" "$WORK_DIR/forge-source" "Forge main source"
    FORGE_SKILLS="$WORK_DIR/forge-source/skills"
    FORGE_SOURCE="the main source, unverified"
fi
FORGE_NAMES=""
USE_CASE_NAMES=""
for src in "$FORGE_SKILLS"/*; do
    [ -f "$src/SKILL.md" ] || continue
    FORGE_NAMES="$FORGE_NAMES $(basename "$src")"
    [ -f "$src/ucs.yaml" ] && USE_CASE_NAMES="$USE_CASE_NAMES $(basename "$src")"
done
[ -n "$USE_CASE_NAMES" ] || die "No Forge skills found in $FORGE_SKILLS"

# Base skills the use-case skills load: every `- name:` under a module's base_skills in ucs.yaml.
REQUIRED_BASE="$(for name in $USE_CASE_NAMES; do
    awk '/^    base_skills:/ {on=1; next} /^[a-z_]+:/ || /^  - id:/ || /^    [a-z_]+:/ {on=0} on && /- name:/ {print $3}' \
        "$FORGE_SKILLS/$name/ucs.yaml"
done | sort -u | tr '\n' ' ')"

# ─── Base skills to request from AI Dev Kit ─────────────────────────────────────
# Named, not the "all" profile: for Claude Code, Codex, and Copilot, "all" installs a plugin, and Forge
# needs the base skills as folders in the same skills directory.
if [ "$FORGE_ONLY" = false ]; then
    aitools_json="$(databricks aitools list -o json 2>/dev/null || true)"
    STABLE_BASE="$(echo "$aitools_json" | awk '
        /"summary":|"agents":/ { exit }
        /"name":/              { gsub(/[",]/, "", $2); name = $2 }
        /"experimental":/      { gsub(/[",]/, "", $2); if (name != "" && $2 == "false") print name; name = "" }' | tr '\n' ' ')"
    BASE_RELEASE="$(echo "$aitools_json" | grep -m1 '"release"' | sed -E 's/.*"release": *"([^"]*)".*/\1/' || true)"
    [ -n "${STABLE_BASE// /}" ] || die "'databricks aitools list -o json' returned no skills. Check the CLI and your network, then rerun."
    missing=""
    for skill in $REQUIRED_BASE; do
        case " $STABLE_BASE " in *" $skill "*) ;; *) missing="$missing $skill" ;; esac
    done
    [ -z "$missing" ] || die "Databricks agent skills ${BASE_RELEASE:-?} lack the base skills Forge needs:$missing. Upgrade the Databricks CLI."
fi

# The MCP server runs from a persistent AI Dev Kit copy; a skills-only install uses a throwaway one.
if [ -n "${FORGE_AIDEVKIT_DIR:-}" ]; then
    AIDK_DIR="$FORGE_AIDEVKIT_DIR"
elif [ "$INSTALL_MCP" = true ]; then
    AIDK_DIR="$FORGE_HOME/ai-dev-kit"
else
    AIDK_DIR="$WORK_DIR/ai-dev-kit"
fi

# ─── Plan ────────────────────────────────────────────────────────────────────────
step "Plan"
msg "Scope:        $SCOPE ($BASE_DIR)"
msg "Agents:       $TOOLS_CSV"
msg "Skills dirs:  $(skill_dirs | tr '\n' ' ')"
if [ "$FORGE_ONLY" = true ]; then
    msg "AI Dev Kit:   skipped (--forge-only); the base skills must already be installed"
else
    if [ -n "${FORGE_AIDEVKIT_DIR:-}" ]; then
        msg "AI Dev Kit:   the copy in $AIDK_DIR"
    elif [ -n "$AIDEVKIT_SHA256" ]; then
        msg "AI Dev Kit:   $AIDEVKIT_REF, checksum pinned, in $AIDK_DIR"
    else
        msg "AI Dev Kit:   $AIDEVKIT_REF, unverified (only $AIDEVKIT_PINNED has a pinned checksum), in $AIDK_DIR"
    fi
    msg "Base skills:  $(echo "$STABLE_BASE" | wc -w | tr -d ' ') stable skills from databricks-agent-skills ${BASE_RELEASE:+$BASE_RELEASE }as folders, plus the MLflow skills"
    if [ "$INSTALL_MCP" = true ]; then
        msg "MCP server:   Databricks MCP server for $TOOLS_CSV, profile $PROFILE"
    else
        msg "MCP server:   skipped (--no-mcp)"
    fi
fi
msg "Forge:        ${FORGE_NAMES# } from $FORGE_SOURCE (replaces existing copies)"
legacy_found=""
while IFS= read -r dir; do
    for name in $LEGACY_SKILLS; do
        if [ -f "$dir/$name/SKILL.md" ] && grep -q '^  forge_kind:' "$dir/$name/SKILL.md"; then
            legacy_found="$legacy_found $dir/$name"
        fi
    done
done < <(skill_dirs)
[ -z "$legacy_found" ] || msg "Removes:      Forge skills installed before the forge- prefix:$legacy_found"
if [[ " $TOOLS " == *" claude "* ]] && command -v claude >/dev/null 2>&1 \
    && claude plugin list 2>/dev/null </dev/null | grep -qF "$CLAUDE_PLUGIN"; then
    warn "The Databricks Claude Code plugin ($CLAUDE_PLUGIN) is installed, so Claude Code will see the base skills twice."
    warn "Forge uses the folders. To remove the plugin copy: claude plugin uninstall $CLAUDE_PLUGIN"
fi

if [ "$DRY_RUN" = true ]; then
    printf '\nDry run: nothing was changed.\n'
    exit 0
fi
if [ "$ASSUME_YES" = false ]; then
    if ( : < /dev/tty ) 2>/dev/null; then
        printf '\nProceed? [y/N] ' > /dev/tty
        read -r answer < /dev/tty || answer=""
        case "$answer" in y|Y|yes|YES) ;; *) msg "Cancelled; nothing was changed."; exit 0 ;; esac
    else
        die "No terminal to confirm on. Rerun with --yes to install without confirming."
    fi
fi

aidk_scope_flag=""
[ "$SCOPE" = "global" ] && aidk_scope_flag="--global"

# ─── 1. AI Dev Kit: base skills and MCP server ───────────────────────────────────
if [ "$FORGE_ONLY" = false ]; then
    step "AI Dev Kit $AIDEVKIT_REF"
    AIDK_PREVIOUS=""
    # Put back the copy an existing MCP server runs from, when this run can't finish replacing it.
    restore_previous_aidk() {
        [ -n "$AIDK_PREVIOUS" ] || return 0
        rm -rf "$AIDK_DIR" && mv "$AIDK_PREVIOUS" "$AIDK_DIR"
        AIDK_PREVIOUS=""
    }
    stamp="$AIDEVKIT_REF ${AIDEVKIT_SHA256:-unverified}"
    if [ -n "${FORGE_AIDEVKIT_DIR:-}" ]; then
        [ -f "$AIDK_DIR/install.sh" ] || die "FORGE_AIDEVKIT_DIR=$AIDK_DIR has no install.sh"
        ok "Using the copy in $AIDK_DIR"
    elif [ -n "$AIDEVKIT_SHA256" ] && [ "$(cat "$AIDK_DIR/.forge-source" 2>/dev/null || true)" = "$stamp" ]; then
        ok "Already have $AIDEVKIT_REF in $AIDK_DIR"
    else
        # Unpack next to the current copy, then swap, so a failed MCP install can put the old one back.
        fetch_archive "$AIDEVKIT_ARCHIVE_URL/$AIDEVKIT_REF" "$AIDK_DIR.new" "AI Dev Kit $AIDEVKIT_REF" "$AIDEVKIT_SHA256"
        [ -f "$AIDK_DIR.new/install.sh" ] || die "The AI Dev Kit $AIDEVKIT_REF archive has no install.sh"
        echo "$stamp" > "$AIDK_DIR.new/.forge-source"
        if [ -e "$AIDK_DIR" ]; then
            rm -rf "$AIDK_DIR.old"
            mv "$AIDK_DIR" "$AIDK_DIR.old"
            AIDK_PREVIOUS="$AIDK_DIR.old"
        fi
        mv "$AIDK_DIR.new" "$AIDK_DIR"
        ok "Downloaded AI Dev Kit $AIDEVKIT_REF${AIDEVKIT_SHA256:+ (checksum verified)}"
    fi

    mlflow_skills="$(sed -n 's/^MLFLOW_SKILLS="\(.*\)"$/\1/p' "$AIDK_DIR/install.sh" | head -1)"
    [ -n "$mlflow_skills" ] || warn "Couldn't read the MLflow skill list from AI Dev Kit; installing the Databricks skills only"
    skills_csv="$(echo "$STABLE_BASE $mlflow_skills" | tr -s ' ' '\n' | sed '/^$/d' | tr '\n' ',' | sed 's/,$//')"

    msg "Installing $(echo "$skills_csv" | tr ',' '\n' | wc -l | tr -d ' ') skills for $TOOLS_CSV (through databricks aitools)..."
    # --silent skips AI Dev Kit's own prompts (this script already confirmed); --force reapplies the named set.
    # shellcheck disable=SC2086  # the scope flag is empty or one word
    if ! (cd "$BASE_DIR" && DEVKIT_SCOPE="$SCOPE" bash "$AIDK_DIR/install.sh" --tools "$TOOLS_CSV" -p "$PROFILE" \
        --skills "$skills_csv" --experimental false --force --silent $aidk_scope_flag); then
        restore_previous_aidk
        die "AI Dev Kit couldn't install the base skills (see its output above)"
    fi
    ok "Base skills installed; manage them with: databricks aitools list|update"

    if [ "$INSTALL_MCP" = true ]; then
        msg "Registering the Databricks MCP server..."
        # shellcheck disable=SC2086
        if (cd "$BASE_DIR" && DEVKIT_SCOPE="$SCOPE" bash "$AIDK_DIR/databricks-mcp-server/mcp_install.sh" \
            --tools "$TOOLS_CSV" -p "$PROFILE" --silent $aidk_scope_flag); then
            ok "Databricks MCP server registered for $TOOLS_CSV (it runs from $AIDK_DIR)"
            [ -z "$AIDK_PREVIOUS" ] || rm -rf "$AIDK_PREVIOUS"
        elif [ -n "$AIDK_PREVIOUS" ]; then
            restore_previous_aidk
            warn "The MCP server didn't install, so the previous AI Dev Kit copy it ran from was put back."
        else
            warn "The MCP server didn't install. The skills don't need it; retry with: bash $AIDK_DIR/databricks-mcp-server/mcp_install.sh"
        fi
    fi
fi

# ─── 2. Forge skills, next to the base skills ────────────────────────────────────
step "Forge skills"
: > "$WORK_DIR/installed"
while IFS= read -r dir; do
    if [ ! -f "$dir/databricks-core/SKILL.md" ]; then
        warn "No base skills in $dir; skipped. Install them first (rerun without --forge-only)."
        continue
    fi
    for name in $FORGE_NAMES; do
        rm -rf "${dir:?}/$name"
        cp -R "$FORGE_SKILLS/$name" "$dir/$name"
        find "$dir/$name" \( -name __pycache__ -o -name .ruff_cache -o -name .pytest_cache \) -prune -exec rm -rf {} +
    done
    for name in $LEGACY_SKILLS; do
        if [ -f "$dir/$name/SKILL.md" ] && grep -q '^  forge_kind:' "$dir/$name/SKILL.md"; then
            rm -rf "${dir:?}/$name"
            ok "Removed the pre-prefix copy $dir/$name"
        fi
    done
    ok "Forge skills → $dir"
    echo "$dir" >> "$WORK_DIR/installed"
done < <(skill_dirs)
[ -s "$WORK_DIR/installed" ] || die "No skills directory had the base skills, so Forge wasn't installed."

# ─── Check: each use-case skill finds its base skills ────────────────────────────
step "Checking"
failed=false
while IFS= read -r dir; do
    for name in $USE_CASE_NAMES; do
        if uv run -q "$dir/$name/scripts/check_base_skills.py" --skills-dir "$dir" > "$WORK_DIR/check.json" 2> "$WORK_DIR/check.err" </dev/null; then
            ok "$name: base skills present ($dir)"
        else
            failed=true
            warn "$name: $(tail -n 1 "$WORK_DIR/check.err")"
        fi
    done
done < "$WORK_DIR/installed"
[ "$failed" = false ] || die "Some use-case skills are missing base skills; see the warnings above."

step "Done"
msg "Restart your agent so it loads the new skills, then type /forge and say what you want to build."

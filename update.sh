#!/bin/bash
# slicefsm updater
# Usage: curl -fsSL https://raw.githubusercontent.com/hionpu/slicefsm/master/update.sh | bash
#   --skill-only | --mcp-only | --target ./proj | --cli a,b

set -e

SCRIPT_VERSION="2026-06-09"
REPO="https://github.com/hionpu/slicefsm"
RAW="https://raw.githubusercontent.com/hionpu/slicefsm/master"
MCP_DIR="$HOME/.local/share/slicefsm"
SKILL_ONLY=false
MCP_ONLY=false
TARGET="."
CLI_LIST=""
REFS="slicing context-scoping"

while [[ $# -gt 0 ]]; do
    case $1 in
        --skill-only) SKILL_ONLY=true; shift ;;
        --mcp-only)   MCP_ONLY=true;  shift ;;
        --target)     TARGET="$2";    shift 2 ;;
        --cli)        CLI_LIST="$2";  shift 2 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

echo "== slicefsm update ($SCRIPT_VERSION) =="
has() { command -v "$1" &>/dev/null; }
fetch() { if has curl; then curl -fsSL "$1"; else wget -qO- "$1"; fi; }

cli_enabled() { if [[ -z "$CLI_LIST" ]]; then return 0; else echo "$CLI_LIST" | tr ',' '\n' | grep -qx "$1"; fi; }

update_mcp() {
    echo "-> MCP server"
    if [[ ! -d "$MCP_DIR/.git" ]]; then
        echo "   not installed at $MCP_DIR — run install.sh first"; exit 1
    fi
    OLD=$(git -C "$MCP_DIR" rev-parse HEAD)
    git -C "$MCP_DIR" pull --quiet
    NEW=$(git -C "$MCP_DIR" rev-parse HEAD)
    if [[ "$OLD" == "$NEW" ]]; then echo "   already up to date"; else
        echo "   $(git -C "$MCP_DIR" log --oneline "$OLD..$NEW" | wc -l | tr -d ' ') new commit(s)"
    fi
    _done=false
    if has uv && uv pip install -e "$MCP_DIR" --system --quiet 2>/dev/null; then _done=true; fi
    if ! $_done; then
        if has pip3; then pip3 install -e "$MCP_DIR" --quiet --break-system-packages 2>/dev/null || pip3 install -e "$MCP_DIR" --quiet
        elif has python3; then python3 -m pip install -e "$MCP_DIR" --quiet --break-system-packages 2>/dev/null || python3 -m pip install -e "$MCP_DIR" --quiet
        else echo "   no pip/uv"; exit 1; fi
    fi
    echo "   package reinstalled"
    echo "   (hooks/MCP point at python -m slicefsm.* — no re-registration needed)"
}

update_skill_files() {
    local dir="$1"
    [[ -d "$dir" ]] || return 1
    mkdir -p "$dir/references"
    fetch "$RAW/skill/SKILL.md" > "$dir/SKILL.md"
    for ref in $REFS; do fetch "$RAW/skill/references/$ref.md" > "$dir/references/$ref.md"; done
    echo "   updated $dir/SKILL.md"
}

update_skill() {
    echo "-> skill files"
    local any=false
    update_skill_files "$TARGET/.claude/skills/slicefsm" && any=true
    update_skill_files "${PI_SKILL_DIR:-$HOME/.pi/agent/skills/slicefsm}" && any=true
    $any || { echo "   skill not installed — run install.sh --skill-only"; exit 1; }
}

if $MCP_ONLY; then update_mcp
elif $SKILL_ONLY; then update_skill
else update_skill; update_mcp; fi

echo ""
echo "Updated. Restart your CLI to pick up changes."

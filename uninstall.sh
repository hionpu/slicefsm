#!/bin/bash
# slicefsm uninstaller
# Usage: curl -fsSL https://raw.githubusercontent.com/hionpu/contractfirst/main/slicefsm/uninstall.sh | bash
#   --skill-only | --mcp-only | --target ./proj

set -e

SCRIPT_VERSION="2026-06-09"
SKILL_ONLY=false
MCP_ONLY=false
TARGET="."

while [[ $# -gt 0 ]]; do
    case $1 in
        --skill-only) SKILL_ONLY=true; shift ;;
        --mcp-only)   MCP_ONLY=true;  shift ;;
        --target)     TARGET="$2";    shift 2 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

echo "== slicefsm uninstall ($SCRIPT_VERSION) =="
has() { command -v "$1" &>/dev/null; }

if ! has python3 && ! $SKILL_ONLY; then
    echo "Error: python3 required."; exit 1
fi

# Remove our hook groups (identified by the slicefsm.hook marker) from a settings file.
HOOK_STRIP_PY='
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
if not path.exists(): sys.exit(0)
try: cfg = json.loads(path.read_text(encoding="utf-8"))
except json.JSONDecodeError: sys.exit(0)
hooks = cfg.get("hooks", {})
MARK = "slicefsm.hook"
changed = False
for ev in list(hooks.keys()):
    kept = []
    for grp in hooks[ev]:
        ours = any(MARK in str(h.get("command", "")) for h in grp.get("hooks", []))
        if ours: changed = True
        else: kept.append(grp)
    if kept: hooks[ev] = kept
    else: hooks.pop(ev); changed = True
if not hooks: cfg.pop("hooks", None)
if changed:
    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8"); print("   hooks removed: " + sys.argv[2])
'

# Remove an MCP server entry by key from a json config.
MCP_STRIP_PY='
import json, pathlib, sys
path, key = pathlib.Path(sys.argv[1]), sys.argv[2]
if not path.exists(): sys.exit(0)
try: cfg = json.loads(path.read_text(encoding="utf-8"))
except json.JSONDecodeError: sys.exit(0)
if "slicefsm" in cfg.get(key, {}):
    cfg[key].pop("slicefsm")
    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8"); print("   deregistered: " + sys.argv[3])
'

uninstall_skill() {
    echo "-> skill"
    rm -rf "$TARGET/.claude/skills/slicefsm" 2>/dev/null && echo "   removed $TARGET/.claude/skills/slicefsm"
    rmdir "$TARGET/.claude/skills" 2>/dev/null || true
    rmdir "$TARGET/.claude" 2>/dev/null || true
    rm -rf "${PI_SKILL_DIR:-$HOME/.pi/agent/skills/slicefsm}" 2>/dev/null || true

    for cfg in "$TARGET/CLAUDE.md" "$TARGET/claude.md" "$TARGET/AGENTS.md" "$TARGET/agents.md" "$TARGET/GEMINI.md" "$TARGET/gemini.md"; do
        [[ -f "$cfg" ]] && grep -q "slicefsm" "$cfg" 2>/dev/null || continue
        sed -i.bak \
            -e '/# slicefsm/d' \
            -e '/@\.claude\/skills\/slicefsm\/SKILL\.md/d' \
            -e '/This project uses the slicefsm harness\./d' \
            -e '/Read \.claude\/skills\/slicefsm\/SKILL\.md/d' \
            "$cfg"
        rm -f "$cfg.bak"; echo "   cleaned $cfg"
    done
}

uninstall_mcp() {
    echo "-> hooks + MCP"
    # Hooks
    python3 -c "$HOOK_STRIP_PY" "$TARGET/.claude/settings.json" "Claude" 2>/dev/null || true
    python3 -c "$HOOK_STRIP_PY" "$HOME/.pi/agent/settings.json" "Pi" 2>/dev/null || true
    python3 -c "$HOOK_STRIP_PY" "$HOME/.config/opencode/opencode.json" "opencode" 2>/dev/null || true

    # MCP registrations
    has claude && { claude mcp remove slicefsm 2>/dev/null && echo "   deregistered: Claude" || true; }
    CFG="$HOME/.codex/config.toml"
    if [[ -f "$CFG" ]] && grep -q "mcp_servers.slicefsm" "$CFG"; then
        sed -i.bak '/^\[mcp_servers\.slicefsm\]/,/^args/d' "$CFG"; rm -f "$CFG.bak"; echo "   deregistered: Codex"
    fi
    python3 -c "$MCP_STRIP_PY" "$HOME/.gemini/settings.json" mcpServers "Gemini" 2>/dev/null || true
    python3 -c "$MCP_STRIP_PY" "$HOME/.pi/agent/mcp.json" mcpServers "Pi" 2>/dev/null || true
    python3 -c "$MCP_STRIP_PY" "$HOME/.config/opencode/opencode.json" mcp "opencode" 2>/dev/null || true

    # Package
    if python3 -c "import slicefsm" &>/dev/null; then
        if has uv; then uv pip uninstall slicefsm --quiet 2>/dev/null || true
        else pip3 uninstall slicefsm -y --quiet 2>/dev/null || pip uninstall slicefsm -y --quiet 2>/dev/null || true; fi
        echo "   package uninstalled"
    fi
    MCP_DIR="$HOME/.local/share/slicefsm"
    [[ -d "$MCP_DIR" ]] && rm -rf "$MCP_DIR" && echo "   removed $MCP_DIR"

    # .harness state: keep by default (user data).
    if [[ -d "$TARGET/.harness" ]]; then
        if [[ -t 0 ]]; then
            read -rp "  Remove harness state at $TARGET/.harness/? [y/N] " yn < /dev/tty
        else
            yn="N"; echo "   keeping $TARGET/.harness/ (remove manually if desired)"
        fi
        [[ "$yn" =~ ^[Yy]$ ]] && rm -rf "$TARGET/.harness" && echo "   removed .harness"
    fi
}

if $MCP_ONLY; then uninstall_mcp
elif $SKILL_ONLY; then uninstall_skill
else uninstall_skill; uninstall_mcp; fi

echo ""
echo "Uninstall complete. Pi hook runtime (@hsingjui/pi-hooks) left in place; remove with: pi uninstall @hsingjui/pi-hooks"

#!/bin/bash
# slicefsm installer
# Usage: curl -fsSL https://raw.githubusercontent.com/hionpu/contractfirst/main/slicefsm/install.sh | bash
# Options:
#   --skill-only        Install skill files only
#   --mcp-only          Install MCP server + hooks only, skip skill
#   --no-hooks          Skip hook registration (MCP + skill only)
#   --target ./proj     Project dir for skill + project-scoped hooks (default: .)
#   --cli a,b,...        Target specific CLIs. Valid: claude, codex, gemini, pi, opencode
#                        Default: auto-detect from PATH

set -e

SCRIPT_VERSION="2026-06-09"
REPO="https://github.com/hionpu/contractfirst"
RAW="https://raw.githubusercontent.com/hionpu/contractfirst/main/slicefsm"
SKILL_ONLY=false
MCP_ONLY=false
NO_HOOKS=false
TARGET="."
CLI_LIST=""
REFS="slicing context-scoping"

while [[ $# -gt 0 ]]; do
    case $1 in
        --skill-only) SKILL_ONLY=true; shift ;;
        --mcp-only)   MCP_ONLY=true;  shift ;;
        --no-hooks)   NO_HOOKS=true;  shift ;;
        --target)     TARGET="$2";    shift 2 ;;
        --cli)        CLI_LIST="$2";  shift 2 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

echo "== slicefsm install ($SCRIPT_VERSION) =="

has() { command -v "$1" &>/dev/null; }
fetch() { if has curl; then curl -fsSL "$1"; else wget -qO- "$1"; fi; }

_VALID_CLIS="claude codex gemini pi opencode"
if [[ -n "$CLI_LIST" ]]; then
    for _name in $(echo "$CLI_LIST" | tr ',' ' '); do
        echo " $_VALID_CLIS " | grep -q " $_name " || { echo "Error: unknown CLI '$_name' (valid: $_VALID_CLIS)"; exit 1; }
    done
    echo "Targeting: $(echo "$CLI_LIST" | tr ',' ' ')"
else
    echo "Auto-detecting CLIs from PATH..."
fi

cli_enabled() {
    if [[ -z "$CLI_LIST" ]]; then has "$1"; else echo "$CLI_LIST" | tr ',' '\n' | grep -qx "$1"; fi
}

if ! has python3 && ! $SKILL_ONLY; then
    echo "Error: python3 required (3.11+). Install it and re-run."; exit 1
fi

# Hooks registered for hook-capable clients only.
HOOK_CLIS="claude pi opencode"

# ── Skill ──────────────────────────────────────────────────────────
install_skill_files() {
    local dir="$1"
    mkdir -p "$dir/references"
    fetch "$RAW/skill/SKILL.md" > "$dir/SKILL.md"
    for ref in $REFS; do fetch "$RAW/skill/references/$ref.md" > "$dir/references/$ref.md"; done
}

install_skill() {
    echo "-> skill files"
    SKILL_DIR="$TARGET/.claude/skills/slicefsm"
    install_skill_files "$SKILL_DIR"
    echo "   skill -> $SKILL_DIR/"

    if cli_enabled pi; then
        PI_SKILL_DIR="${PI_SKILL_DIR:-$HOME/.pi/agent/skills/slicefsm}"
        install_skill_files "$PI_SKILL_DIR"
        echo "   Pi skill -> $PI_SKILL_DIR/"
    fi

    if cli_enabled claude; then
        _md="$TARGET/CLAUDE.md"; [[ -f "$TARGET/claude.md" ]] && _md="$TARGET/claude.md"
        [[ -f "$_md" ]] || touch "$_md"
        grep -q "slicefsm" "$_md" 2>/dev/null || printf '\n# slicefsm\n@.claude/skills/slicefsm/SKILL.md\n' >> "$_md"
        echo "   imported in $_md (Claude)"
    fi

    if cli_enabled codex || cli_enabled pi || cli_enabled opencode; then
        _md="$TARGET/AGENTS.md"; [[ -f "$TARGET/agents.md" ]] && _md="$TARGET/agents.md"
        [[ -f "$_md" ]] || touch "$_md"
        grep -q "slicefsm" "$_md" 2>/dev/null || printf '\n# slicefsm\nThis project uses the slicefsm harness.\nRead .claude/skills/slicefsm/SKILL.md before any code change.\n' >> "$_md"
        echo "   imported in $_md (Codex/Pi/opencode)"
    fi

    if cli_enabled gemini; then
        _md="$TARGET/GEMINI.md"; [[ -f "$TARGET/gemini.md" ]] && _md="$TARGET/gemini.md"
        [[ -f "$_md" ]] || touch "$_md"
        grep -q "slicefsm" "$_md" 2>/dev/null || printf '\n# slicefsm\n@.claude/skills/slicefsm/SKILL.md\n' >> "$_md"
        echo "   imported in $_md (Gemini)"
    fi
}

# ── MCP server ─────────────────────────────────────────────────────
install_mcp() {
    echo "-> MCP server"
    MCP_DIR="$HOME/.local/share/slicefsm"
    if [[ -d "$MCP_DIR/.git" ]]; then git -C "$MCP_DIR" pull --quiet; else git clone --quiet "$REPO" "$MCP_DIR"; fi

    _done=false
    if has uv && uv pip install -e "$MCP_DIR/slicefsm" --system --quiet 2>/dev/null; then _done=true; fi
    if ! $_done; then
        if has pip3; then pip3 install -e "$MCP_DIR/slicefsm" --quiet --break-system-packages 2>/dev/null || pip3 install -e "$MCP_DIR/slicefsm" --quiet
        elif has python3; then python3 -m pip install -e "$MCP_DIR/slicefsm" --quiet --break-system-packages 2>/dev/null || python3 -m pip install -e "$MCP_DIR/slicefsm" --quiet
        else echo "  Error: no pip/uv"; exit 1; fi
    fi
    echo "   package -> $MCP_DIR/slicefsm"

    if cli_enabled claude; then
        if has claude; then
            claude mcp add slicefsm --scope project -- python -m slicefsm.server 2>/dev/null \
                && echo "   registered (Claude)" \
                || echo "   ! manual: claude mcp add slicefsm --scope project -- python -m slicefsm.server"
        fi
    fi
    if cli_enabled codex; then
        mkdir -p "$HOME/.codex"; CFG="$HOME/.codex/config.toml"; [[ -f "$CFG" ]] || touch "$CFG"
        grep -q "mcp_servers.slicefsm" "$CFG" 2>/dev/null || printf '\n[mcp_servers.slicefsm]\ncommand = "python"\nargs = ["-m", "slicefsm.server"]\n' >> "$CFG"
        echo "   registered (Codex)"
    fi
    for pair in "gemini:.gemini/settings.json:mcpServers" "pi:.pi/agent/mcp.json:mcpServers" "opencode:.config/opencode/opencode.json:mcp"; do
        name="${pair%%:*}"; rest="${pair#*:}"; rel="${rest%%:*}"; key="${rest##*:}"
        cli_enabled "$name" || continue
        python3 - "$rel" "$key" "$name" <<'PYEOF'
import json, pathlib, sys
rel, key, name = sys.argv[1], sys.argv[2], sys.argv[3]
p = pathlib.Path.home() / rel
p.parent.mkdir(parents=True, exist_ok=True)
try: cfg = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
except json.JSONDecodeError: cfg = {}
servers = cfg.setdefault(key, {})
if name == "opencode":
    entry = {"type": "local", "command": ["python", "-m", "slicefsm.server"]}
elif name == "pi":
    entry = {"command": "python", "args": ["-m", "slicefsm.server"], "lifecycle": "lazy", "idleTimeout": 10}
else:
    entry = {"command": "python", "args": ["-m", "slicefsm.server"]}
if servers.get("slicefsm") != entry:
    servers["slicefsm"] = entry
    p.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    print(f"   registered ({name})")
else:
    print(f"   present ({name})")
PYEOF
    done
}

# ── Hooks (the enforcement layer; hook-capable clients only) ───────
HOOK_MERGE_PY='
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
path.parent.mkdir(parents=True, exist_ok=True)
try: cfg = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
except json.JSONDecodeError: cfg = {}
hooks = cfg.setdefault("hooks", {})
MARK = "slicefsm.hook"
def cmd(ev): return f"python -m slicefsm.hook {ev}"
ADD = {
  "UserPromptSubmit": [{"hooks": [{"type": "command", "command": cmd("userpromptsubmit")}]}],
  "PreToolUse": [{"matcher": ".*", "hooks": [{"type": "command", "command": cmd("pretooluse")}]}],
  "PostToolUse": [{"matcher": "edit|write|Edit|Write|MultiEdit|NotebookEdit", "hooks": [{"type": "command", "command": cmd("posttooluse")}]}],
  "Stop": [{"hooks": [{"type": "command", "command": cmd("stop")}]}],
}
def present(ev):
    for grp in hooks.get(ev, []):
        for h in grp.get("hooks", []):
            if MARK in str(h.get("command", "")): return True
    return False
changed = False
for ev, groups in ADD.items():
    if not present(ev):
        hooks.setdefault(ev, []).extend(groups); changed = True
if changed:
    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8"); print("   hooks registered: " + sys.argv[2])
else:
    print("   hooks present: " + sys.argv[2])
'

install_hooks() {
    $NO_HOOKS && { echo "-> hooks skipped (--no-hooks)"; return; }
    echo "-> hooks (enforcement)"
    if cli_enabled claude; then
        python3 -c "$HOOK_MERGE_PY" "$TARGET/.claude/settings.json" "Claude (project)"
    fi
    if cli_enabled pi; then
        python3 -c "$HOOK_MERGE_PY" "$HOME/.pi/agent/settings.json" "Pi (global)"
        if has pi; then
            pi install npm:@hsingjui/pi-hooks 2>/dev/null && echo "   pi-hooks extension installed" \
                || echo "   ! install the hook runtime: pi install npm:@hsingjui/pi-hooks"
        else
            echo "   ! Pi needs the hook runtime: pi install npm:@hsingjui/pi-hooks"
        fi
    fi
    if cli_enabled opencode; then
        python3 -c "$HOOK_MERGE_PY" "$HOME/.config/opencode/opencode.json" "opencode (best-effort)"
    fi
    if cli_enabled codex || cli_enabled gemini; then
        echo "   note: codex/gemini have no hook system — they run MCP + skill only (no hard enforcement)."
    fi
}

# ── Run ────────────────────────────────────────────────────────────
if $MCP_ONLY; then
    install_mcp; install_hooks
elif $SKILL_ONLY; then
    install_skill
else
    install_skill; install_mcp; install_hooks
fi

echo ""
echo "Done. Next:"
echo "  1. Open your CLI in this directory."
echo "  2. Describe a feature; the AI calls submit_feature and proposes slices."
echo "  3. Approve in the terminal:  harness approve   (or: python -m slicefsm.cli approve)"
echo "Docs: $REPO/tree/main/slicefsm"

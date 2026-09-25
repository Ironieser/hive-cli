#!/usr/bin/env bash
# install.sh — install hive-cli
# Usage: bash install.sh
#        HIVE_INSTALL_DIR=~/my/path bash install.sh

set -euo pipefail

REPO_URL="git@github.com:Ironieser/hive-cli.git"
INSTALL_DIR="${HIVE_INSTALL_DIR:-${HOME}/.local/share/hive-cli}"
BIN_DIR="${HOME}/bin"
HIVE_DIR="${HOME}/.hive"
OLD_CACHE="${HOME}/.cache"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BOLD='\033[1m'; NC='\033[0m'

info()  { echo -e "  ${GREEN}✓${NC} $*"; }
warn()  { echo -e "  ${YELLOW}!${NC} $*"; }
title() { echo -e "\n${BOLD}$*${NC}"; }

# ── 1. Clone or update ────────────────────────────────────────────────────────
title "1. Installing hive-cli"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ "${SCRIPT_DIR}" != "${INSTALL_DIR}" && -f "${SCRIPT_DIR}/hive" && -d "${SCRIPT_DIR}/libexec" ]]; then
    # Running from a local checkout that ISN'T the install dir → deploy THIS code.
    # (Takes precedence over a git update so local/unpushed changes actually install;
    # excludes .git and gitignored scratch so we don't clobber the install dir's repo.)
    info "Installing from local checkout: ${SCRIPT_DIR}"
    mkdir -p "$INSTALL_DIR"
    # Feedback filed through the INSTALLED copy (older hive-feedback wrote to
    # <install-dir>/feedback/inbox) must reach the checkout, not be wiped by the
    # rsync --delete below. Rescue anything the checkout doesn't have yet.
    if [[ -d "${INSTALL_DIR}/feedback/inbox" ]]; then
        mkdir -p "${SCRIPT_DIR}/feedback/inbox"
        _rescued=0
        for _fb in "${INSTALL_DIR}"/feedback/inbox/*.md; do
            [[ -f "$_fb" ]] || continue
            if [[ ! -e "${SCRIPT_DIR}/feedback/inbox/$(basename "$_fb")" ]]; then
                cp "$_fb" "${SCRIPT_DIR}/feedback/inbox/" && _rescued=$(( _rescued + 1 ))
            fi
        done
        (( _rescued > 0 )) && warn "Rescued ${_rescued} feedback report(s) from ${INSTALL_DIR}/feedback/inbox into the checkout (run: hive feedback reindex)"
    fi
    if command -v rsync >/dev/null 2>&1; then
        rsync -a --delete --exclude '.git' --exclude 'feedback/inbox/' \
              --filter=':- .gitignore' \
              "${SCRIPT_DIR}/" "${INSTALL_DIR}/"
    else
        # Fallback: copy tracked top-level entries (no .git)
        for _item in "${SCRIPT_DIR}"/* "${SCRIPT_DIR}"/.gitignore; do
            [[ -e "$_item" ]] && cp -r "$_item" "${INSTALL_DIR}/"
        done
    fi
elif [[ -d "${INSTALL_DIR}/.git" ]]; then
    info "Updating existing installation at ${INSTALL_DIR} from origin..."
    git -C "$INSTALL_DIR" fetch origin
    git -C "$INSTALL_DIR" reset --hard origin/main
else
    info "Cloning from ${REPO_URL}..."
    git clone "$REPO_URL" "$INSTALL_DIR"
fi

# Record where this install came from so `hive feedback` (run from the installed
# copy by other agents) files reports into the checkout the maintainer triages.
if [[ "${SCRIPT_DIR}" != "${INSTALL_DIR}" && -f "${SCRIPT_DIR}/hive" && -d "${SCRIPT_DIR}/libexec" ]]; then
    printf '%s\n' "${SCRIPT_DIR}" > "${INSTALL_DIR}/.source_checkout"
    info "Feedback from the installed copy will be filed into ${SCRIPT_DIR}/feedback"
else
    rm -f "${INSTALL_DIR}/.source_checkout"
fi

# ── 2. Make executables + ensure data files present ──────────────────────────
title "2. Setting permissions"
chmod +x "${INSTALL_DIR}/hive"
chmod +x "${INSTALL_DIR}/libexec"/hive-*
info "All binaries are executable"

# Copy data files that git reset/clone might miss if repo was installed via cp
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ "${SCRIPT_DIR}" != "${INSTALL_DIR}" && -f "${SCRIPT_DIR}/pool_config.example.json" ]]; then
    cp "${SCRIPT_DIR}/pool_config.example.json" "${INSTALL_DIR}/pool_config.example.json"
    info "Synced pool_config.example.json"
fi


# ── 3. Ensure ~/bin exists ────────────────────────────────────────────────────
title "3. Linking to ~/bin"
mkdir -p "$BIN_DIR"

ln -sf "${INSTALL_DIR}/hive" "${BIN_DIR}/hive"
info "Linked: ${BIN_DIR}/hive → ${INSTALL_DIR}/hive"

# ── 4. Backward-compat symlinks ───────────────────────────────────────────────
for name in myjob mynode jobtop; do
    target="${BIN_DIR}/${name}"
    if [[ -L "$target" ]]; then
        ln -sf "${INSTALL_DIR}/hive" "$target"
        info "Updated compat link: ${target}"
    elif [[ ! -e "$target" ]]; then
        ln -sf "${INSTALL_DIR}/hive" "$target"
        info "Created compat link: ${target}"
    else
        warn "Skipped ${target}: existing file preserved (not a symlink)"
    fi
done

# ── 5. Stop old mynode-daemon if running ─────────────────────────────────────
title "4. Checking for old daemon"
OLD_PIDS=$(pgrep -f "mynode-daemon" 2>/dev/null || true)
if [[ -n "$OLD_PIDS" ]]; then
    warn "Found running mynode-daemon (PID: $OLD_PIDS) — stopping it..."
    echo "$OLD_PIDS" | xargs -r kill -TERM 2>/dev/null || true
    sleep 1
    info "Old daemon stopped"
else
    info "No old mynode-daemon running"
fi

# ── 6. Migrate existing cache data ───────────────────────────────────────────
title "5. Migrating data to ~/.hive/"
mkdir -p "$HIVE_DIR"
for f in node_monitor.json node_monitor.pid node_monitor.log; do
    old="${OLD_CACHE}/${f}"
    new="${HIVE_DIR}/${f}"
    if [[ -f "$old" && ! -f "$new" ]]; then
        cp "$old" "$new"
        info "Migrated: ${old} → ${new}"
    fi
done
# Remove the PID file ONLY if its daemon is genuinely dead. Read just the first line:
# the file is the 2-line cluster-singleton format "<pid>\n<host>", so `cat` would feed
# "<pid>\n<host>" to kill -0 (always fails) and wrongly delete a *live* poller's pid file.
if [[ -f "${HIVE_DIR}/node_monitor.pid" ]]; then
    pid=$(head -1 "${HIVE_DIR}/node_monitor.pid" 2>/dev/null || true)
    if [[ -n "$pid" ]] && ! kill -0 "$pid" 2>/dev/null; then
        rm -f "${HIVE_DIR}/node_monitor.pid"
        info "Removed stale PID file"
    fi
fi

# ── 7. Install Claude Code skill (optional) ──────────────────────────────────
title "6. Claude Code skill"
# Standard skill layout: SKILL.md (frontmatter + short body, always loaded when the
# skill triggers) + references/ (read on demand). Installed to ~/.claude/skills/hive/.
SKILL_SRC_DIR="${INSTALL_DIR}/.claude/skills/hive"
SKILL_DST_DIR="${HOME}/.claude/skills/hive"
if [[ -f "${SKILL_SRC_DIR}/SKILL.md" ]]; then
    mkdir -p "$SKILL_DST_DIR"
    if command -v rsync >/dev/null 2>&1; then
        rsync -a --delete "${SKILL_SRC_DIR}/" "${SKILL_DST_DIR}/"
    else
        rm -rf "${SKILL_DST_DIR:?}"/* && cp -r "${SKILL_SRC_DIR}/." "${SKILL_DST_DIR}/"
    fi
    info "Installed skill: ${SKILL_DST_DIR}/SKILL.md  (+references; invoke as /hive in Claude Code)"
else
    warn "Skill dir not found in install dir — skipping"
fi
# The pre-v0.4.1 skill was a single slash-command file. Having both would surface two
# /hive entries, so park the old one (renamed, not deleted, in case it was customised).
OLD_SKILL="${HOME}/.claude/commands/hive.md"
if [[ -f "$OLD_SKILL" ]]; then
    mv "$OLD_SKILL" "${OLD_SKILL}.pre-skill.bak"
    warn "Moved old slash-command skill to ${OLD_SKILL}.pre-skill.bak (superseded by ~/.claude/skills/hive/)"
fi

# ── 8. PATH reminder ─────────────────────────────────────────────────────────
echo ""
if echo "$PATH" | tr ':' '\n' | grep -qx "$BIN_DIR"; then
    info "${BIN_DIR} is already in PATH"
else
    warn "${BIN_DIR} is not in PATH. Add to your shell profile:"
    echo "    export PATH=\"\${HOME}/bin:\${PATH}\""
fi

echo ""
echo -e "${BOLD}hive-cli installed successfully.${NC}"
echo -e "  Run: ${GREEN}hive help${NC}"
echo ""

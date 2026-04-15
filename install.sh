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
if [[ -d "${INSTALL_DIR}/.git" ]]; then
    info "Updating existing installation at ${INSTALL_DIR}..."
    # Reset any untracked/modified files that would block pull, then update
    git -C "$INSTALL_DIR" fetch origin
    git -C "$INSTALL_DIR" reset --hard origin/main
else
    # If running from a local checkout, copy instead of clone
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    if [[ -f "${SCRIPT_DIR}/hive" && -d "${SCRIPT_DIR}/libexec" ]]; then
        info "Installing from local checkout: ${SCRIPT_DIR}"
        if [[ "${SCRIPT_DIR}" != "${INSTALL_DIR}" ]]; then
            mkdir -p "$INSTALL_DIR"
            cp -r "${SCRIPT_DIR}/." "${INSTALL_DIR}/"
        fi
    else
        info "Cloning from ${REPO_URL}..."
        git clone "$REPO_URL" "$INSTALL_DIR"
    fi
fi

# ── 2. Make executables ───────────────────────────────────────────────────────
title "2. Setting permissions"
chmod +x "${INSTALL_DIR}/hive"
chmod +x "${INSTALL_DIR}/libexec"/hive-*
info "All binaries are executable"

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
# Stale pid from old daemon is now invalid — remove it
if [[ -f "${HIVE_DIR}/node_monitor.pid" ]]; then
    pid=$(cat "${HIVE_DIR}/node_monitor.pid" 2>/dev/null || true)
    if [[ -n "$pid" ]] && ! kill -0 "$pid" 2>/dev/null; then
        rm -f "${HIVE_DIR}/node_monitor.pid"
        info "Removed stale PID file"
    fi
fi

# ── 7. PATH reminder ─────────────────────────────────────────────────────────
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

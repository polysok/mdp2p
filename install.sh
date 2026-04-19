#!/usr/bin/env sh
# mdp2p installer for macOS and Linux.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/<user>/mdp2p/main/install.sh | sh
#
# Environment overrides:
#   MDP2P_REPO    = GitHub repo in "owner/name" form   (default: polysok/mdp2p)
#   MDP2P_VERSION = tag to install                     (default: latest)
#   INSTALL_DIR   = where to drop the binary           (default: ~/.local/bin)

set -eu

REPO="${MDP2P_REPO:-polysok/mdp2p}"
INSTALL_DIR="${INSTALL_DIR:-$HOME/.local/bin}"
VERSION="${MDP2P_VERSION:-latest}"

# ─── Platform detection ───────────────────────────────────────────────
uname_s=$(uname -s)
uname_m=$(uname -m)

case "$uname_s" in
    Linux*)  os_tag="linux"  ;;
    Darwin*) os_tag="macos"  ;;
    *)
        printf "Unsupported OS: %s\nWindows users: use install.ps1 in PowerShell.\n" "$uname_s" >&2
        exit 1
        ;;
esac

case "$uname_m" in
    x86_64|amd64)   arch_tag="x86_64" ;;
    arm64|aarch64)  arch_tag="arm64"  ;;
    *)
        printf "Unsupported architecture: %s\n" "$uname_m" >&2
        exit 1
        ;;
esac

# Linux arm64 builds are not produced yet; fall back with a clear message
# instead of downloading the wrong asset.
if [ "$os_tag" = "linux" ] && [ "$arch_tag" = "arm64" ]; then
    printf "No Linux ARM binary yet.\n" >&2
    printf "Build from source: https://github.com/%s\n" "$REPO" >&2
    exit 1
fi

asset="mdp2p-${os_tag}-${arch_tag}"

if [ "$VERSION" = "latest" ]; then
    url="https://github.com/${REPO}/releases/latest/download/${asset}"
else
    url="https://github.com/${REPO}/releases/download/${VERSION}/${asset}"
fi

# ─── Download ─────────────────────────────────────────────────────────
printf "→ Downloading %s (%s)\n  from %s\n" "$asset" "$VERSION" "$url"

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

if command -v curl >/dev/null 2>&1; then
    curl --fail --location --progress-bar --output "$tmp/mdp2p" "$url"
elif command -v wget >/dev/null 2>&1; then
    wget --quiet --show-progress --output-document="$tmp/mdp2p" "$url"
else
    printf "Error: curl or wget is required.\n" >&2
    exit 1
fi

chmod +x "$tmp/mdp2p"

# ─── macOS Gatekeeper quarantine strip ────────────────────────────────
# Binaries downloaded via curl/wget inherit com.apple.quarantine, which
# forces the user to right-click → Open on first launch. We strip it so
# the CLI "just runs". This only works on unsigned binaries coming from
# our own script — malware-class bypasses still hit notarization.
if [ "$os_tag" = "macos" ] && command -v xattr >/dev/null 2>&1; then
    xattr -d com.apple.quarantine "$tmp/mdp2p" 2>/dev/null || true
fi

# ─── Install ──────────────────────────────────────────────────────────
mkdir -p "$INSTALL_DIR"
target="$INSTALL_DIR/mdp2p"

if [ -e "$target" ]; then
    printf "  Replacing existing %s\n" "$target"
fi
mv "$tmp/mdp2p" "$target"

printf "✓ Installed to %s\n" "$target"

# ─── PATH hint ────────────────────────────────────────────────────────
case ":$PATH:" in
    *":$INSTALL_DIR:"*)
        printf "\nRun: mdp2p\n"
        ;;
    *)
        shell_name=$(basename "${SHELL:-}")
        case "$shell_name" in
            zsh)  rc="~/.zshrc"   ;;
            bash) rc="~/.bashrc"  ;;
            fish) rc="~/.config/fish/config.fish" ;;
            *)    rc="your shell's rc file" ;;
        esac

        printf "\n"
        printf "⚠  %s is not in your PATH.\n" "$INSTALL_DIR"
        printf "   Add it with:\n\n"
        if [ "$shell_name" = "fish" ]; then
            printf "       echo 'set -gx PATH %s \$PATH' >> %s\n\n" "$INSTALL_DIR" "$rc"
        else
            printf "       echo 'export PATH=\"%s:\$PATH\"' >> %s\n\n" "$INSTALL_DIR" "$rc"
        fi
        printf "   then open a new terminal and run: mdp2p\n"
        ;;
esac

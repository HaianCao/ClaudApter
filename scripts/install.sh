#!/usr/bin/env bash
# Install or update ClaudApter (claude-ca) for the current user on Linux or macOS.
#
#   curl -fsSL https://raw.githubusercontent.com/HaianCao/ClaudApter/main/scripts/install.sh | bash
#   bash scripts/install.sh [--non-interactive] [--no-path-changes] [--skip-cli-install]
#                           [--protoverta-wheel PATH] [--source-branch NAME]
#
# No administrator access is used. Each update is built in a new environment
# and only then made current, so a failed update keeps the previous install.
set -euo pipefail

ADAPTER_NAME="ClaudApter"
ADAPTER_REPO="HaianCao/ClaudApter"
ADAPTER_PACKAGE="claudapter"
ADAPTER_COMMAND="claude-ca"
ADAPTER_VERSION="0.3.0"
CLI_NAME="Claude Code"
CLI_COMMAND="claude"
CLI_INSTALLER="https://claude.ai/install.sh"
# Arguments run once after installing, to migrate older settings ("" for none).
POST_INSTALL_CHECK=""
PROTOVERTA_VERSION="0.4.0"
PROTOVERTA_SHA256="14aba71545b4689c10823fa5b6cde75f2af08d744c6942687a26f447c69ac5cd"

non_interactive=0
path_changes=1
skip_cli=0
local_wheel=""
source_branch=""
temp_root=""
lock_dir=""

usage() { sed -n '2,9p' "${BASH_SOURCE[0]:-/dev/null}" 2>/dev/null | sed 's/^# \{0,1\}//'; }
step() { printf '\n==> %s\n' "$1"; }
fail() { printf '\nSetup failed: %s\n' "$1" >&2; exit 1; }
cleanup() {
    if [ -n "$lock_dir" ]; then rm -rf "$lock_dir"; fi
    if [ -n "$temp_root" ]; then rm -rf "$temp_root"; fi
}
trap cleanup EXIT

while [ $# -gt 0 ]; do
    case "$1" in
        --non-interactive) non_interactive=1 ;;
        --no-path-changes) path_changes=0 ;;
        --skip-cli-install) skip_cli=1 ;;
        --protoverta-wheel) [ $# -ge 2 ] || fail "--protoverta-wheel needs a path."; local_wheel="$2"; shift ;;
        --source-branch) [ $# -ge 2 ] || fail "--source-branch needs a name."; source_branch="$2"; shift ;;
        -h | --help) usage; exit 0 ;;
        *) fail "Unknown option: $1 (see --help)." ;;
    esac
    shift
done

confirm() {
    # Piped installs (curl | bash) read the answer from the terminal.
    [ "$non_interactive" -eq 1 ] && return 0
    { : < /dev/tty; } 2> /dev/null || return 0
    local answer=""
    printf '%s [y/N] ' "$1" > /dev/tty
    read -r answer < /dev/tty || answer=""
    case "$answer" in y | Y | yes | YES | Yes) return 0 ;; *) return 1 ;; esac
}

fetch() {
    if command -v curl > /dev/null 2>&1; then
        curl -fsSL --retry 2 --connect-timeout 20 --max-time 300 -o "$2" "$1"
    elif command -v wget > /dev/null 2>&1; then
        wget -q -T 60 -O "$2" "$1"
    else
        fail "curl or wget is required to download files."
    fi
}

sha256_of() {
    if command -v sha256sum > /dev/null 2>&1; then
        sha256sum "$1" | cut -d ' ' -f 1
    else
        shasum -a 256 "$1" | cut -d ' ' -f 1
    fi
}

is_adapter_source() {
    [ -f "$1/pyproject.toml" ] && grep -Eq "^name[[:space:]]*=[[:space:]]*\"$ADAPTER_PACKAGE\"[[:space:]]*$" "$1/pyproject.toml"
}

find_python() {
    local candidate path
    for candidate in python3.13 python3.12 python3.11 python3.14 python3 python; do
        path="$(command -v "$candidate" 2> /dev/null)" || continue
        if "$path" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' 2> /dev/null; then
            printf '%s' "$path"
            return 0
        fi
    done
    return 1
}

case "$(uname -s)" in
    Linux) platform="Linux" ;;
    Darwin) platform="macOS" ;;
    *) fail "This installer supports Linux and macOS. On Windows, use scripts/install.ps1." ;;
esac
if [ -n "$source_branch" ]; then
    case "$source_branch" in
        *..* | -* | "") fail "--source-branch must be a valid Git branch name." ;;
    esac
    printf '%s' "$source_branch" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9._/-]*$' || fail "--source-branch must be a valid Git branch name."
fi

data_home="${XDG_DATA_HOME:-$HOME/.local/share}"
case "$data_home" in /*) ;; *) data_home="$HOME/.local/share" ;; esac
install_root="$data_home/$ADAPTER_COMMAND"
bin_dir="$HOME/.local/bin"
temp_root="$(mktemp -d "${TMPDIR:-/tmp}/$ADAPTER_PACKAGE-install.XXXXXX")"

repo_root=""
if [ -z "$source_branch" ]; then
    script_path="${BASH_SOURCE[0]:-}"
    if [ -n "$script_path" ] && [ -f "$script_path" ]; then
        candidate="$(cd "$(dirname "$script_path")/.." && pwd)"
        if is_adapter_source "$candidate"; then repo_root="$candidate"; fi
    fi
fi

echo "This installs or updates $ADAPTER_NAME ($ADAPTER_COMMAND) for your $platform user."
echo "The official $CLI_NAME CLI is installed if it is missing. No administrator access is used."
[ -n "$repo_root" ] || echo "The $ADAPTER_NAME source will be downloaded from the project GitHub repository."
confirm "Continue?" || { echo "Nothing was changed."; exit 0; }

if [ -z "$repo_root" ]; then
    step "Downloading $ADAPTER_NAME source from GitHub"
    if [ -n "$source_branch" ]; then
        archive="https://github.com/$ADAPTER_REPO/archive/refs/heads/$source_branch.tar.gz"
    else
        # A remote install fetches the release this installer belongs to.
        archive="https://github.com/$ADAPTER_REPO/archive/refs/tags/v$ADAPTER_VERSION.tar.gz"
    fi
    fetch "$archive" "$temp_root/source.tar.gz" || fail "Could not download $archive."
    mkdir "$temp_root/source"
    tar -xzf "$temp_root/source.tar.gz" -C "$temp_root/source"
    for candidate in "$temp_root"/source/*; do
        if is_adapter_source "$candidate"; then repo_root="$candidate"; fi
    done
    [ -n "$repo_root" ] || fail "The downloaded archive does not contain a $ADAPTER_NAME project."
fi

step "Checking Python 3.11 or newer"
python="$(find_python)" || {
    if [ "$platform" = "macOS" ]; then
        fail "Python 3.11+ was not found. Install it (brew install python@3.13, or https://www.python.org/downloads/macos/) and run this installer again."
    fi
    fail "Python 3.11+ was not found. Install it with your package manager (for example: sudo apt install python3 python3-venv) and run this installer again."
}
echo "Using Python: $python"
"$python" -c 'import ensurepip, venv' 2> /dev/null || fail "This Python cannot create environments. Install its venv support (for example: sudo apt install python3-venv) and run this installer again."

step "Checking the official $CLI_NAME CLI"
if [ "$skip_cli" -eq 1 ]; then
    echo "Skipped the official CLI installation (--skip-cli-install)."
elif command -v "$CLI_COMMAND" > /dev/null 2>&1 || [ -x "$bin_dir/$CLI_COMMAND" ]; then
    echo "The official CLI is already available; left unchanged."
else
    step "Installing the official $CLI_NAME CLI"
    fetch "$CLI_INSTALLER" "$temp_root/official-install.sh" || fail "Could not download the official installer ($CLI_INSTALLER)."
    bash "$temp_root/official-install.sh" < /dev/null || fail "The official $CLI_NAME installer failed."
    if ! command -v "$CLI_COMMAND" > /dev/null 2>&1 && [ ! -x "$bin_dir/$CLI_COMMAND" ]; then
        fail "The official installer finished, but '$CLI_COMMAND' was not found. Open a new terminal, check '$CLI_COMMAND --version', then run this installer again."
    fi
fi

mkdir -p "$install_root/environments" "$bin_dir"
for target in "$install_root" "$install_root/environments"; do
    [ -L "$target" ] && fail "Refusing to install through a linked directory ($target)."
done
lock_dir="$install_root/install.lock"
if ! mkdir "$lock_dir" 2> /dev/null; then
    owner="$(cat "$lock_dir/pid" 2> /dev/null || true)"
    if [ -n "$owner" ] && kill -0 "$owner" 2> /dev/null; then
        lock_dir=""
        fail "Another $ADAPTER_NAME installation is running. Wait for it to finish and retry."
    fi
    rm -rf "$lock_dir"
    mkdir "$lock_dir" || { lock_dir=""; fail "Could not lock $install_root."; }
fi
echo $$ > "$lock_dir/pid"

if [ -f "$install_root/current.txt" ]; then step "Updating $ADAPTER_NAME"; else step "Installing $ADAPTER_NAME"; fi

if [ -n "$local_wheel" ]; then
    case "$local_wheel" in *.whl) ;; *) fail "--protoverta-wheel must point to a local Protoverta wheel file." ;; esac
    [ -f "$local_wheel" ] || fail "--protoverta-wheel must point to a local Protoverta wheel file."
    wheel="$(cd "$(dirname "$local_wheel")" && pwd)/$(basename "$local_wheel")"
else
    printf '%s' "$PROTOVERTA_SHA256" | grep -Eq '^[0-9a-f]{64}$' || fail "This installer has no pinned Protoverta hash (unreleased build). Pass --protoverta-wheel PATH with a locally built wheel of the same version."
    wheel="$temp_root/protoverta-$PROTOVERTA_VERSION-py3-none-any.whl"
    asset="https://github.com/HaianCao/Protoverta/releases/download/v$PROTOVERTA_VERSION/protoverta-$PROTOVERTA_VERSION-py3-none-any.whl"
    fetch "$asset" "$wheel" || fail "The pinned Protoverta release asset could not be downloaded ($asset). The previous installation was preserved."
    actual="$(sha256_of "$wheel")"
    [ "$actual" = "$PROTOVERTA_SHA256" ] || fail "The downloaded Protoverta wheel does not match its pinned SHA-256 (expected $PROTOVERTA_SHA256, got $actual). The previous installation was preserved."
fi

identity="$("$python" -c 'import uuid; print(uuid.uuid4().hex)')"
candidate="$install_root/environments/$identity"
"$python" -I -m venv "$candidate" || fail "Could not create a Python environment. The previous installation was preserved."
candidate_python="$candidate/bin/python"
"$candidate_python" -I -m pip install --quiet --disable-pip-version-check "$repo_root" "$wheel" || fail "Package installation failed. The previous installation was preserved."
"$candidate_python" -I -m pip check || fail "The dependency check failed. The previous installation was preserved."
"$candidate_python" -I -m "$ADAPTER_PACKAGE.cli" --help > /dev/null || fail "The $ADAPTER_COMMAND entry point check failed. The previous installation was preserved."
"$candidate_python" -I -c 'import protoverta.server' || fail "The Protoverta gateway check failed. The previous installation was preserved."

# Publish the new environment with one atomic rename.
printf '%s\n' "$identity" > "$install_root/current.tmp"
mv -f "$install_root/current.tmp" "$install_root/current.txt"

quoted_root="$(printf '%s' "$install_root" | sed "s/'/'\\\\''/g")"
cat > "$bin_dir/.$ADAPTER_COMMAND.tmp" << EOF
#!/bin/sh
# $ADAPTER_NAME launcher (written by install.sh). Runs the active environment.
root='$quoted_root'
id=\$(tr -d '\r\n' < "\$root/current.txt" 2> /dev/null)
case "\$id" in
    *[!0-9a-f]* | "") echo "No usable $ADAPTER_NAME environment. Run the installer again." >&2; exit 1 ;;
esac
[ "\${#id}" -eq 32 ] || { echo "No usable $ADAPTER_NAME environment. Run the installer again." >&2; exit 1; }
exec "\$root/environments/\$id/bin/python" -I -m $ADAPTER_PACKAGE.cli "\$@"
EOF
chmod 755 "$bin_dir/.$ADAPTER_COMMAND.tmp"
mv -f "$bin_dir/.$ADAPTER_COMMAND.tmp" "$bin_dir/$ADAPTER_COMMAND"

# Keep the current and the newest previous environment; older ones are unused.
"$python" -I - "$install_root/environments" "$identity" << 'PRUNE'
import pathlib, re, shutil, sys
root, current = pathlib.Path(sys.argv[1]), sys.argv[2]
old = [p for p in root.iterdir() if p.is_dir() and not p.is_symlink() and re.fullmatch("[0-9a-f]{32}", p.name) and p.name != current]
for stale in sorted(old, key=lambda p: p.stat().st_mtime, reverse=True)[1:]:
    shutil.rmtree(stale, ignore_errors=True)
PRUNE

if [ "$path_changes" -eq 1 ]; then
    case ":$PATH:" in
        *":$bin_dir:"*) ;;
        *)
            block_start="# >>> $ADAPTER_COMMAND PATH >>>"
            for profile in "$HOME/.profile" "$HOME/.bashrc" "$HOME/.zshrc"; do
                case "$profile" in
                    "$HOME/.profile") ;;
                    "$HOME/.zshrc") [ -f "$profile" ] || [ "$platform" = "macOS" ] || continue ;;
                    *) [ -f "$profile" ] || continue ;;
                esac
                if ! grep -Fq "$block_start" "$profile" 2> /dev/null; then
                    {
                        printf '\n%s\n' "$block_start"
                        printf 'case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH" ;; esac\n'
                        printf '# <<< %s PATH <<<\n' "$ADAPTER_COMMAND"
                    } >> "$profile"
                fi
            done
            echo "Added ~/.local/bin to PATH in your shell profile."
            ;;
    esac
fi

if [ -n "$POST_INSTALL_CHECK" ]; then
    # shellcheck disable=SC2086 # Intentional word splitting of fixed arguments.
    "$bin_dir/$ADAPTER_COMMAND" $POST_INSTALL_CHECK > /dev/null 2>&1 || echo "Could not validate the existing settings; they were preserved. Run '$ADAPTER_COMMAND setup' to check them."
fi

echo
echo "$ADAPTER_NAME is ready. Open a new terminal and run: $ADAPTER_COMMAND"
echo "(Setup opens the first time; '$ADAPTER_COMMAND setup' changes providers later.)"

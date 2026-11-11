#!/usr/bin/env bash
# Remove ClaudApter (claude-ca) installed by install.sh on Linux or macOS.
#
#   curl -fsSL https://raw.githubusercontent.com/HaianCao/ClaudApter/main/scripts/uninstall.sh | bash
#   bash scripts/uninstall.sh [--delete-data] [--non-interactive] [--no-path-changes]
#
# The official CLI is never removed. Provider settings, API keys and the model
# cache are kept unless --delete-data is given; custom paths are never deleted.
set -euo pipefail

ADAPTER_NAME="ClaudApter"
ADAPTER_PACKAGE="claudapter"
ADAPTER_COMMAND="claude-ca"
ENV_PREFIX="CLAUDE_CA"

delete_data=0
non_interactive=0
path_changes=1

fail() { printf '\nUninstall failed: %s\n' "$1" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --delete-data) delete_data=1 ;;
        --non-interactive) non_interactive=1 ;;
        --no-path-changes) path_changes=0 ;;
        -h | --help) sed -n '2,8p' "${BASH_SOURCE[0]:-/dev/null}" 2> /dev/null | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) fail "Unknown option: $1 (see --help)." ;;
    esac
    shift
done

confirm() {
    [ "$non_interactive" -eq 1 ] && return 0
    { : < /dev/tty; } 2> /dev/null || return 0
    local answer=""
    printf '%s [y/N] ' "$1" > /dev/tty
    read -r answer < /dev/tty || answer=""
    case "$answer" in y | Y | yes | YES | Yes) return 0 ;; *) return 1 ;; esac
}

absolute_or_default() {
    case "$1" in /*) printf '%s' "$1" ;; *) printf '%s' "$2" ;; esac
}

data_home="$(absolute_or_default "${XDG_DATA_HOME:-}" "$HOME/.local/share")"
config_home="$(absolute_or_default "${XDG_CONFIG_HOME:-}" "$HOME/.config")"
cache_home="$(absolute_or_default "${XDG_CACHE_HOME:-}" "$HOME/.cache")"
install_root="$data_home/$ADAPTER_COMMAND"
launcher="$HOME/.local/bin/$ADAPTER_COMMAND"

echo "This removes $ADAPTER_NAME only, not the official coding CLI."
if [ "$delete_data" -eq 1 ]; then
    echo "Provider settings, API keys and the model cache will be deleted too (--delete-data)."
else
    echo "Provider settings and API keys are kept."
fi
confirm "Continue?" || { echo "Nothing was changed."; exit 0; }

if [ -f "$launcher" ] && grep -q "launcher (written by install.sh)" "$launcher" 2> /dev/null; then
    rm -f "$launcher"
    echo "Removed $launcher."
fi
if [ -d "$install_root" ] && [ ! -L "$install_root" ]; then
    if [ -f "$install_root/current.txt" ] || [ -d "$install_root/environments" ]; then
        rm -rf "${install_root:?}"
        echo "Removed $ADAPTER_NAME program files."
    fi
fi

if [ "$path_changes" -eq 1 ]; then
    for profile in "$HOME/.profile" "$HOME/.bashrc" "$HOME/.zshrc"; do
        [ -f "$profile" ] || continue
        grep -Fq "# >>> $ADAPTER_COMMAND PATH >>>" "$profile" || continue
        # Remove only the block install.sh added, plus the blank line before it.
        awk -v start="# >>> $ADAPTER_COMMAND PATH >>>" -v stop="# <<< $ADAPTER_COMMAND PATH <<<" '
            $0 == start { skip = 1; if (held) held = 0; next }
            skip && $0 == stop { skip = 0; next }
            skip { next }
            { if (held) print ""; if ($0 == "") { held = 1; next } held = 0; print }
            END { if (held) print "" }
        ' "$profile" > "$profile.$ADAPTER_PACKAGE.tmp" && mv "$profile.$ADAPTER_PACKAGE.tmp" "$profile"
    done
else
    echo "PATH left unchanged (--no-path-changes)."
fi

if [ "$delete_data" -eq 1 ]; then
    for name in "${ENV_PREFIX}_CONFIG" "${ENV_PREFIX}_CACHE" "${ENV_PREFIX}_ENV_FILE"; do
        if [ -n "${!name:-}" ]; then
            echo "Kept the custom path in $name; custom paths are never deleted."
        fi
    done
    for target in "$config_home/$ADAPTER_PACKAGE" "$cache_home/$ADAPTER_PACKAGE"; do
        if [ -d "$target" ] && [ ! -L "$target" ]; then
            rm -rf "${target:?}"
            echo "Deleted $target."
        fi
    done
else
    echo "Kept provider settings, API keys and cache in $config_home/$ADAPTER_PACKAGE and $cache_home/$ADAPTER_PACKAGE."
fi
echo "$ADAPTER_NAME uninstall complete. Open a new terminal to refresh PATH."

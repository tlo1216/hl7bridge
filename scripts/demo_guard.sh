# Deletion guard for scripts/demo.sh. Sourced, not run.
#
# The demo owns one disposable data folder. When it creates the folder it
# writes a marker file. On the next run it deletes the folder only when:
#   - the marker file is there (so this script made it),
#   - the path is a real directory, not a symlink,
#   - the path is not /, $HOME, the repo root, the current folder, or a
#     folder that contains the current folder.
# Anything else stops the script with a message and deletes nothing.

DEMO_MARKER=".hl7bridge-demo"

demo_die() {
    echo "demo.sh: $*" >&2
    exit 1
}

# Print the physical path of an existing directory.
demo_realdir() {
    (cd -- "$1" 2>/dev/null && pwd -P)
}

prepare_data_dir() {
    local dir="$1"
    local repo_root="${2:-}"
    [ -n "$dir" ] || demo_die "DATA_DIR is empty"

    if [ -L "$dir" ]; then
        demo_die "refusing to use $dir: it is a symlink"
    fi

    if [ -e "$dir" ]; then
        [ -d "$dir" ] || demo_die "refusing to use $dir: it exists and is not a folder"
        local real here home root
        real="$(demo_realdir "$dir")" || demo_die "cannot resolve $dir"
        here="$(pwd -P)"
        home="$(demo_realdir "${HOME:-/}")"
        root=""
        if [ -n "$repo_root" ]; then
            root="$(demo_realdir "$repo_root")"
        fi
        if [ "$real" = "/" ] || [ "$real" = "$home" ] || [ "$real" = "$here" ] \
            || { [ -n "$root" ] && [ "$real" = "$root" ]; }; then
            demo_die "refusing to delete $dir ($real): it is /, your home, the repo root or the current folder"
        fi
        case "$here/" in
            "$real"/*) demo_die "refusing to delete $dir ($real): it contains the current folder" ;;
        esac
        if [ ! -f "$real/$DEMO_MARKER" ]; then
            demo_die "refusing to delete $dir: it has no $DEMO_MARKER marker, so the demo did not create it. Remove it yourself or set DATA_DIR to a new folder."
        fi
        rm -rf -- "$real"
    fi

    mkdir -p -- "$dir"
    printf 'Created by scripts/demo.sh. Holds synthetic data only. Safe to delete.\n' \
        > "$dir/$DEMO_MARKER"
}

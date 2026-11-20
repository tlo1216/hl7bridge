# Deletion guard and process helper for scripts/demo.sh. Sourced, not run.
#
# The demo owns one disposable data folder. It proves ownership by creating
# the folder itself with a plain mkdir, which fails if anything is already
# there, and only then writes a marker file into it. On a later run it
# deletes the folder only when:
#   - the path has no ".." component (so "missing/../existing" cannot reach
#     a folder the -e test never saw),
#   - the path, with trailing slashes removed, is not a symlink ("link/"
#     would otherwise follow the link and pass the test),
#   - the marker file is there,
#   - the folder is not /, a drive root, and is neither the same folder as
#     nor a parent of $HOME, the repo root or the current folder. These are
#     compared with -ef (device and inode), so case, 8.3 short names and
#     symlinks cannot make one folder look like another.
# Anything else stops the script with a message and deletes nothing.

DEMO_MARKER=".hl7bridge-demo"
DEMO_MARKER_TEXT="Created by scripts/demo.sh. Holds synthetic data only. Safe to delete."

demo_die() {
    echo "demo.sh: $*" >&2
    exit 1
}

# Print the physical path of an existing directory. CDPATH is cleared so a
# relative path never resolves against some other folder.
demo_realdir() {
    (CDPATH='' cd -- "$1" 2>/dev/null && pwd -P)
}

# True when directory $1 is directory $2 or one of its parents.
demo_is_same_or_parent() {
    local candidate="$1" p up
    p="$(demo_realdir "$2")" || return 1
    while :; do
        if [ "$p" -ef "$candidate" ]; then
            return 0
        fi
        case "$p" in
            / | "") return 1 ;;
        esac
        up="${p%/*}"
        [ -n "$up" ] || up="/"
        [ "$up" != "$p" ] || return 1
        p="$up"
    done
}

demo_on_windows() {
    case "$(uname -s 2>/dev/null)" in
        MINGW* | MSYS* | CYGWIN*) return 0 ;;
    esac
    return 1
}

prepare_data_dir() {
    local dir="$1"
    local repo_root="${2:-}"
    [ -n "$dir" ] || demo_die "DATA_DIR is empty"

    if demo_on_windows; then
        # Git Bash accepts both separators; check one spelling.
        dir="${dir//\\//}"
    fi
    # Drop trailing slashes, so "link/" is tested as the link itself.
    while [ "$dir" != "/" ] && [ "${dir%/}" != "$dir" ]; do
        dir="${dir%/}"
    done
    case "/$dir/" in
        */../*) demo_die "refusing to use $dir: it has a .. component; give a plain path" ;;
    esac

    if [ -L "$dir" ]; then
        demo_die "refusing to use $dir: it is a symlink"
    fi

    if [ -e "$dir" ]; then
        [ -d "$dir" ] || demo_die "refusing to use $dir: it exists and is not a folder"
        local real base
        real="$(demo_realdir "$dir")" || demo_die "cannot resolve $dir"
        case "$real" in
            / | /[A-Za-z] | [A-Za-z]:/ | [A-Za-z]:)
                demo_die "refusing to delete $dir ($real): it is a filesystem or drive root"
                ;;
        esac
        if [ / -ef "$real" ]; then
            demo_die "refusing to delete $dir ($real): it is the filesystem root"
        fi
        for base in "$(pwd -P)" "${HOME:-/}" "$repo_root"; do
            [ -n "$base" ] && [ -d "$base" ] || continue
            if demo_is_same_or_parent "$real" "$base"; then
                demo_die "refusing to delete $dir ($real): it is, or holds, your home, the repo root or the current folder"
            fi
        done
        if [ ! -f "$real/$DEMO_MARKER" ] || [ -L "$real/$DEMO_MARKER" ]; then
            demo_die "refusing to delete $dir: it has no $DEMO_MARKER marker, so the demo did not create it. Remove it yourself or set DATA_DIR to a new folder."
        fi
        # `hl7bridge demo` writes a JSON marker and deletes only the files it
        # lists. This script deletes the whole folder, so it accepts only the
        # marker it wrote itself.
        local first_line=""
        IFS= read -r first_line < "$real/$DEMO_MARKER" || true
        case "$first_line" in
            "$DEMO_MARKER_TEXT"*) ;;
            *) demo_die "refusing to delete $dir: its $DEMO_MARKER marker was not written by scripts/demo.sh. Use hl7bridge demo with that folder, or set DATA_DIR to a new folder." ;;
        esac
        rm -rf -- "$real"
        [ ! -e "$dir" ] || demo_die "could not remove $dir"
    fi

    # Ownership: create the parent if needed, then the folder itself with a
    # plain mkdir. If anything appeared at the path in the meantime, mkdir
    # fails and nothing is marked.
    local parent
    case "$dir" in
        */*) parent="${dir%/*}" ;;
        *) parent="." ;;
    esac
    [ -n "$parent" ] || parent="/"
    mkdir -p -- "$parent" || demo_die "cannot create $parent"
    mkdir -- "$dir" 2>/dev/null \
        || demo_die "refusing to use $dir: it appeared while the demo was starting, so this run did not create it"
    [ ! -L "$dir" ] || demo_die "refusing to use $dir: it is a symlink"
    (
        set -o noclobber
        printf '%s\n' "$DEMO_MARKER_TEXT" > "$dir/$DEMO_MARKER"
    ) || demo_die "cannot write the marker in $dir"
}

# Stop a background process started by demo.sh, and every process it
# started. On Windows (Git Bash), $! names the launcher (uv, or the venv's
# python.exe), and killing it leaves its Python child alive, still holding
# the MLLP port; taskkill /T ends the whole tree.
demo_stop_pid() {
    local pid="$1" winpid=""
    [ -n "$pid" ] || return 0
    if demo_on_windows && [ -r "/proc/$pid/winpid" ]; then
        winpid="$(cat "/proc/$pid/winpid" 2>/dev/null || true)"
        if [ -n "$winpid" ]; then
            taskkill //F //T //PID "$winpid" >/dev/null 2>&1 || true
        fi
    fi
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
}

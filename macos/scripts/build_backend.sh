#!/usr/bin/env bash
# Assemble Resources/backend: relocatable CPython + site-packages + the app's own
# Python source.
#
# Two-step on purpose. We build a wheelhouse first, then install from it with
# --no-index, so that every artifact entering the bundle can be audited by
# filename: it must be either py3-none-any (pure Python) or an arm64 macOS
# wheel. An sdist that only gets built at install time would slip past that
# check, and an sdist for a *native* package means "needs a compiler on the
# user's machine", which a shipped app does not have (plan §3).

. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

BACKEND_DIR="${1:-$BUILD_DIR/backend}"

PY_ROOT="$CACHE_DIR/python-$PY_FULL-$PBS_TAG"
[ -x "$PY_ROOT/bin/python3" ] || bash "$MACOS_DIR/scripts/fetch_python.sh"

WHEELHOUSE="$CACHE_DIR/wheelhouse"

# ---------------------------------------------------------------- wheelhouse --
log "building wheelhouse"
mkdir -p "$WHEELHOUSE"
"$PY_ROOT/bin/python3" -m pip wheel \
  --quiet --wheel-dir "$WHEELHOUSE" \
  $BUNDLE_REQUIREMENTS

# --------------------------------------------------------------------- audit --
# Every wheel must be pure Python or arm64-compatible. universal2 and abi3 are
# fine. A `linux`/`x86_64`-only wheel or a leftover .tar.gz fails the build
# rather than shipping something that won't load.
log "auditing wheelhouse"
audit_failed=0
for whl in "$WHEELHOUSE"/*; do
  name="$(basename "$whl")"
  case "$name" in
    *-py3-none-any.whl|*-py2.py3-none-any.whl|*-none-any.whl) ;;
    *macosx_*_arm64.whl|*macosx_*_universal2.whl) ;;
    *.whl)
      warn "unusable wheel for $TARGET_ARCH macOS: $name"
      audit_failed=1
      ;;
    *)
      warn "not a wheel: $name"
      audit_failed=1
      ;;
  esac
done
[ "$audit_failed" -eq 0 ] || die "wheelhouse audit failed; see warnings above"

# ------------------------------------------------------------------- assemble --
log "assembling $BACKEND_DIR"
rm -rf "$BACKEND_DIR"
mkdir -p "$BACKEND_DIR"

cp -R "$PY_ROOT" "$BACKEND_DIR/python"

# Trim what a shipped app never uses. Keeps the bundle smaller and, more
# usefully, shrinks the set of binaries that need signing.
rm -rf "$BACKEND_DIR/python/lib/python$PY_MM/test" \
       "$BACKEND_DIR/python/lib/python$PY_MM/idlelib" \
       "$BACKEND_DIR/python/lib/python$PY_MM/tkinter" \
       "$BACKEND_DIR/python/lib/python$PY_MM/turtledemo" \
       "$BACKEND_DIR/python/lib/python$PY_MM/lib2to3" \
       "$BACKEND_DIR/python/lib/python$PY_MM/site-packages/pip" \
       "$BACKEND_DIR/python/lib/python$PY_MM/site-packages/setuptools" \
       "$BACKEND_DIR/python/lib/python$PY_MM/site-packages/pkg_resources" \
       "$BACKEND_DIR/python/share" \
       "$BACKEND_DIR/python/include"
find "$BACKEND_DIR/python/lib/python$PY_MM/site-packages" -maxdepth 1 \
     \( -name 'pip-*' -o -name 'setuptools-*' \) -prune -exec rm -rf {} + 2>/dev/null || true

# Tcl/Tk. The bundle has no GUI Python, and each of these dylibs would otherwise
# be one more binary to sign, verify and notarize for nothing.
find "$BACKEND_DIR/python" -name '_tkinter*.so' -delete
rm -rf "$BACKEND_DIR/python/lib/tcl"* "$BACKEND_DIR/python/lib/tk"* \
       "$BACKEND_DIR/python/lib/itcl"* "$BACKEND_DIR/python/lib/itk"* \
       "$BACKEND_DIR/python/lib/thread"* "$BACKEND_DIR/python/lib/sqlite3"* \
       "$BACKEND_DIR/python/lib/libtcl"* "$BACKEND_DIR/python/lib/libtk"*

# Console-script wrappers. The shell only ever launches bootstrap.py, and these
# hardcode the build machine's interpreter path in their shebang.
find "$BACKEND_DIR/python/bin" -type f ! -name 'python3.*' -delete
find "$BACKEND_DIR/python/bin" -type l ! -name 'python3' -delete

find "$BACKEND_DIR/python" -name '__pycache__' -type d -prune -exec rm -rf {} +

log "installing dependencies into site-packages"
"$PY_ROOT/bin/python3" -m pip install \
  --quiet --no-index --find-links "$WHEELHOUSE" \
  --target "$BACKEND_DIR/site-packages" \
  $BUNDLE_REQUIREMENTS

find "$BACKEND_DIR/site-packages" -name '__pycache__' -type d -prune -exec rm -rf {} +
# pip leaves per-distribution console scripts in a bin/ dir under --target; the
# shell never invokes them and they hardcode the build machine's paths.
rm -rf "$BACKEND_DIR/site-packages/bin"

log "copying application source"
# Only the app's own Python and the *bundled public* plugin templates. The
# private plugin directory is deliberately never bundled (plan §4); the shell
# points FINANCE_PLUGINS_DIR at a user-chosen path.
mkdir -p "$BACKEND_DIR/app"
for item in src rules; do
  cp -R "$REPO_ROOT/$item" "$BACKEND_DIR/app/$item"
done
mkdir -p "$BACKEND_DIR/app/plugins"
# Tracked template plugins only -- never whatever happens to be sitting in the
# working tree, which on a real machine is the owner's private parsers.
( cd "$REPO_ROOT" && git ls-files plugins ) | while read -r tracked; do
  mkdir -p "$BACKEND_DIR/app/$(dirname "$tracked")"
  cp "$REPO_ROOT/$tracked" "$BACKEND_DIR/app/$tracked"
done
find "$BACKEND_DIR/app" -name '__pycache__' -type d -prune -exec rm -rf {} +
find "$BACKEND_DIR/app" -name '*.pyc' -delete

cp "$MACOS_DIR/payload/bootstrap.py" "$BACKEND_DIR/bootstrap.py"
cp "$MACOS_DIR/payload/import_smoke.py" "$BACKEND_DIR/import_smoke.py"

# ------------------------------------------------------------------ manifest --
{
  echo "python: $PY_FULL+$PBS_TAG (aarch64-apple-darwin)"
  echo "arch:   $TARGET_ARCH"
  echo "built:  $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "commit: $(cd "$REPO_ROOT" && git rev-parse --short HEAD)"
  echo "--- wheels ---"
  ls "$WHEELHOUSE" | sort
  echo "--- native extensions ---"
  ( cd "$BACKEND_DIR" && find . \( -name '*.so' -o -name '*.dylib' \) | sort )
} > "$BACKEND_DIR/MANIFEST.txt"

native_count=$( (cd "$BACKEND_DIR" && find . \( -name '*.so' -o -name '*.dylib' \) ) | wc -l | tr -d ' ')
log "done: $(du -sh "$BACKEND_DIR" | cut -f1), $native_count native binaries to sign"
log "manifest: $BACKEND_DIR/MANIFEST.txt"

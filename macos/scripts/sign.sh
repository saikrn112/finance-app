#!/usr/bin/env bash
# Code-sign an app bundle, innermost binaries first.
#
# `codesign --deep` is not a substitute for signing inner binaries yourself, and
# every .so/.dylib the bundled interpreter loads must carry a signature the
# hardened runtime accepts. Order matters: nested code first, bundle last,
# because signing the bundle seals a hash of everything inside it.
#
# The bundled interpreter gets its own entitlements. It -- not the app shell --
# is the host process that dlopens the wheels, and library validation is decided
# by the host process's entitlements. See Resources/Entitlements-Interpreter*.plist.
#
# Usage: sign.sh <path-to-.app> [identity]
#
# Identity defaults to "-" (ad-hoc). Ad-hoc is fine for development and breaks
# two things you will hit (plan §3): TCC grants and Keychain ACLs are keyed to
# the signature, so every rebuild re-prompts, and notarization is impossible.
# Set MACOS_SIGN_IDENTITY once a Developer ID exists.

. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

APP="${1:?usage: sign.sh <path-to-.app> [identity]}"
IDENTITY="${2:-${MACOS_SIGN_IDENTITY:--}}"

APP_ENTITLEMENTS="$MACOS_DIR/Resources/Entitlements.plist"

# Library validation matches on Team ID, and only a Developer ID certificate has one.
# Ad-hoc has none, and neither does a self-signed "FinanceApp Local" identity -- so both
# need the permissive interpreter entitlements. Keyed on the identity *name* rather than
# on `= "-"`, because a self-signed identity silently took the strict path before and
# every bundled extension module failed to load.
case "$IDENTITY" in
  "Developer ID Application"*|"Apple Development"*)
    PY_ENTITLEMENTS="$MACOS_DIR/Resources/Entitlements-Interpreter.plist"
    HAS_TEAM_ID=1
    ;;
  *)
    PY_ENTITLEMENTS="$MACOS_DIR/Resources/Entitlements-Interpreter-adhoc.plist"
    HAS_TEAM_ID=0
    ;;
esac

[ -d "$APP" ] || die "not a bundle: $APP"
[ -f "$APP_ENTITLEMENTS" ] || die "missing $APP_ENTITLEMENTS"
[ -f "$PY_ENTITLEMENTS" ] || die "missing $PY_ENTITLEMENTS"

# --timestamp needs a round-trip to Apple's timestamp server and is only meaningful for a
# certificate Apple issued.
if [ "$HAS_TEAM_ID" -eq 1 ]; then
  TS_FLAG="--timestamp"
elif [ "$IDENTITY" = "-" ]; then
  TS_FLAG="--timestamp=none"
  warn "ad-hoc signature: not notarizable, and TCC/Keychain grants reset on every rebuild."
  warn "  For a stable identity that survives rebuilds, see macos/README.md (Signing)."
else
  TS_FLAG="--timestamp=none"
  log "self-signed identity '$IDENTITY': stable across rebuilds, still not notarizable"
fi

sign_one() {
  # sign_one <file> [entitlements]
  if [ -n "${2:-}" ]; then
    codesign --force $TS_FLAG --options runtime --entitlements "$2" --sign "$IDENTITY" "$1"
  else
    codesign --force $TS_FLAG --options runtime --sign "$IDENTITY" "$1"
  fi
}

log "signing nested code in $(basename "$APP") with identity: $IDENTITY"

# 1. Extension modules and dylibs. -print0/read -d '' so a path with a space
#    cannot split into two arguments.
lib_count=0
while IFS= read -r -d '' f; do
  sign_one "$f" >/dev/null
  lib_count=$((lib_count + 1))
done < <(find "$APP" \( -name '*.so' -o -name '*.dylib' \) -print0)
log "  $lib_count extension modules / dylibs"

# 2. Nested Mach-O executables (the interpreter, and anything else that turns up).
#    Skipping symlinks: signing through one signs the target twice and the second
#    pass can race the first.
exe_count=0
while IFS= read -r -d '' f; do
  case "$(file -b "$f")" in
    *Mach-O*)
      case "$f" in
        */python/bin/python3.*)
          sign_one "$f" "$PY_ENTITLEMENTS" >/dev/null
          log "  interpreter: $(basename "$f") <- $(basename "$PY_ENTITLEMENTS")"
          ;;
        *)
          sign_one "$f" >/dev/null
          ;;
      esac
      exe_count=$((exe_count + 1))
      ;;
  esac
done < <(find "$APP/Contents/Resources" -type f ! -type l -perm -u+x \
             ! -name '*.so' ! -name '*.dylib' -print0 2>/dev/null)
log "  $exe_count nested executables"

# 3. The bundle itself, last.
log "signing the bundle"
sign_one "$APP" "$APP_ENTITLEMENTS" >/dev/null

log "verifying"
codesign --verify --deep --strict --verbose=2 "$APP"
log "signed: $APP"

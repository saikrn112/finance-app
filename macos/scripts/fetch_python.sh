#!/usr/bin/env bash
# Download and cache a relocatable CPython (python-build-standalone).
#
# We use an explicit standalone layout rather than py2app/PyInstaller because
# those produce opaque bundles that are hard to sign, notarize and debug. This
# is more work up front and far more inspectable (plan §1).

. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

mkdir -p "$CACHE_DIR"
archive="$CACHE_DIR/$PBS_ASSET"

if [ ! -f "$archive" ]; then
  log "downloading $PBS_ASSET"
  curl -fL --retry 3 -o "$archive.part" "$PBS_URL"
  mv "$archive.part" "$archive"
else
  log "using cached $PBS_ASSET"
fi

# Record the digest so a silently-swapped cache entry is detectable.
shasum -a 256 "$archive" | tee "$CACHE_DIR/$PBS_ASSET.sha256"

extract_root="$CACHE_DIR/python-$PY_FULL-$PBS_TAG"
if [ ! -x "$extract_root/bin/python3" ]; then
  log "extracting to $extract_root"
  rm -rf "$extract_root" "$extract_root.tmp"
  mkdir -p "$extract_root.tmp"
  tar -xzf "$archive" -C "$extract_root.tmp"
  # The archive contains a single top-level `python/` directory.
  mv "$extract_root.tmp/python" "$extract_root"
  rm -rf "$extract_root.tmp"
fi

log "interpreter: $extract_root/bin/python3"
"$extract_root/bin/python3" -V

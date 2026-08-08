#!/usr/bin/env bash
# Deploy a locally built FreqUI into the bots' served directory.
#
# Why a script rather than `cp -r dist/* .../installed/`:
#
# `cp` never removes anything, so every deploy leaves the previous build's hashed chunks
# behind. That directory had accumulated 66 stale DashboardViewCustom chunks and grown to
# 443 MB. Disk is the lesser problem — a browser holding a reference to a chunk from three
# builds ago will happily be served it, which produces incoherent behaviour that looks
# like a code bug and is very hard to trace back to a deploy.
#
# So `assets/` is replaced wholesale, and only after the new build has been checked to
# exist: a failed build must not be able to empty the served directory.
set -euo pipefail

FREQUI="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../frequi" && pwd)}"
TARGET="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/freqtrade/rpc/api_server/ui/installed"

DIST="$FREQUI/dist"
if [ ! -f "$DIST/index.html" ] || [ ! -d "$DIST/assets" ]; then
  echo "No usable build in $DIST — run 'npm run build' in $FREQUI first." >&2
  exit 1
fi
if [ ! -d "$TARGET" ]; then
  echo "Target $TARGET does not exist — is this a freqtrade checkout?" >&2
  exit 1
fi

before=$(du -sm "$TARGET" 2>/dev/null | cut -f1)
rm -rf "$TARGET/assets"
cp -r "$DIST"/* "$TARGET/"
after=$(du -sm "$TARGET" | cut -f1)

echo "Deployed $(basename "$FREQUI") → ui/installed  (${before} MB → ${after} MB)"
echo "No bot restart needed: static files are read per request. Hard-reload the browser."

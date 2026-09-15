#!/bin/bash
# Rebuild the app with the existing local environment. No package/model downloads.
set -euo pipefail
SORITAKER_PATCH_DIR="$(cd "$(dirname "$0")" && pwd)"
exec /bin/bash "$SORITAKER_PATCH_DIR/Install.command" --update-only

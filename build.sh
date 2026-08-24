#!/usr/bin/env bash
# build.sh — validate and pack the Apple Calendar MCP desktop extension
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${SCRIPT_DIR}/dist"

echo "=== Apple Calendar MCP — build ==="

# Check for mcpb CLI
if ! command -v mcpb &>/dev/null; then
    echo "Installing mcpb CLI…"
    npm install -g @anthropic-ai/mcpb
fi

# Gate on version drift between pyproject.toml and manifest.json.
PYPROJECT_VERSION=$(grep '^version' "${SCRIPT_DIR}/pyproject.toml" | head -1 | sed 's/version = "\(.*\)"/\1/')
MANIFEST_VERSION=$(python3 -c "import json;print(json.load(open('${SCRIPT_DIR}/manifest.json'))['version'])")
if [[ "${PYPROJECT_VERSION}" != "${MANIFEST_VERSION}" ]]; then
    echo "✗ Version drift: pyproject.toml=${PYPROJECT_VERSION} manifest.json=${MANIFEST_VERSION}" >&2
    exit 1
fi

# Validate
echo ""
echo "Validating manifest…"
mcpb validate "${SCRIPT_DIR}/manifest.json"
echo "✓ Manifest valid."

# Pack
echo ""
mkdir -p "${OUT}"
VERSION="${PYPROJECT_VERSION}"
STABLE="${OUT}/apple-calendar.mcpb"
VERSIONED="${OUT}/apple-calendar-${VERSION}.mcpb"
mcpb pack "${SCRIPT_DIR}" "${STABLE}"

# Make the version visible in Finder: Spotlight comment, kMDItemVersion,
# and a versioned filename copy (the bulletproof display that survives
# copy/upload/quarantine stripping the xattrs).
cp -f "${STABLE}" "${VERSIONED}"
for F in "${STABLE}" "${VERSIONED}"; do
    osascript -e "tell application \"Finder\" to set comment of (POSIX file \"${F}\" as alias) to \"Apple Calendar MCP — v${VERSION}\"" >/dev/null 2>&1 || true
    xattr -w "com.apple.metadata:kMDItemVersion" "${VERSION}" "${F}" 2>/dev/null || true
    mdimport "${F}" 2>/dev/null || true
done

echo ""
echo "✓ Built:"
echo "    ${STABLE}        (stable name — drag-install target)"
echo "    ${VERSIONED}    (versioned name — visible version in filename)"
echo ""
echo "To install: double-click the .mcpb file, or drag it into Claude Desktop."
echo ""
echo "ℹ  macOS will prompt for Calendar access on first use. You can also pre-grant"
echo "   under System Settings → Privacy & Security → Calendars (Full Access)."

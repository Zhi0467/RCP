#!/bin/sh
# Install or replace /Applications/RCP.app from the latest desktop release.
#
#   curl -fsSL https://raw.githubusercontent.com/Zhi0467/RCP/main/scripts/install-macos.sh | sh
#
# A file that curl downloads carries no quarantine flag, so macOS opens the
# app without the Open Anyway approval. The script relies only on release asset
# names, so a copy newer than the latest release still installs that release.
set -eu

releases=${RCP_RELEASES_URL:-https://github.com/Zhi0467/RCP/releases}
applications=${RCP_APPLICATIONS_DIR:-/Applications}
app="$applications/RCP.app"

fail() {
  printf 'RCP install: %s\n' "$1" >&2
  exit 1
}

# hw.optional.arm64 is 1 on Apple Silicon even in a Rosetta shell, and absent on Intel.
[ "$(uname -s)" = Darwin ] || fail "RCP's prebuilt app needs macOS."
[ "$(sysctl -n hw.optional.arm64 2>/dev/null || true)" = 1 ] ||
  fail "RCP's prebuilt app needs an Apple Silicon Mac."
major=$(sw_vers -productVersion | cut -d. -f1)
[ "$major" -ge 13 ] 2>/dev/null || fail "RCP needs macOS 13 or later."

if pgrep -f '/RCP\.app/Contents/MacOS/' >/dev/null 2>&1; then
  fail "RCP is running. Quit it with Cmd+Q, then run this command again."
fi
[ -w "$applications" ] ||
  fail "$applications is not writable. Run this command from an admin account."

latest=$(curl -fsS -o /dev/null -w '%{redirect_url}' "$releases/latest") ||
  fail "could not reach the RCP releases page."
tag=${latest##*/}
printf '%s\n' "$tag" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+$' ||
  fail "could not read the latest RCP version."
zip="RCP-$tag-macos-arm64.zip"

download=$(mktemp -d "${TMPDIR:-/tmp}/rcp-install.XXXXXX")
# The new app is unpacked beside the old one, so the swap is a rename on one volume.
stage=$(mktemp -d "$applications/.rcp-install.XXXXXX")
cleanup() {
  # An interruption between the two renames must not lose the old app.
  if [ ! -e "$app" ] && [ -e "$stage/RCP.previous.app" ]; then
    mv "$stage/RCP.previous.app" "$app" || {
      printf 'RCP install: the old app is at %s\n' "$stage/RCP.previous.app" >&2
      rm -rf "$download"
      return
    }
  fi
  rm -rf "$download" "$stage"
}
trap cleanup EXIT
trap 'exit 130' INT TERM HUP

printf 'Downloading RCP %s...\n' "$tag"
for name in "$zip" "$zip.sha256"; do
  curl -fsSL -o "$download/$name" "$releases/download/desktop-$tag/$name" ||
    fail "the macOS app for $tag is not published yet. Try again later."
done
(cd "$download" && shasum -a 256 -c "$zip.sha256" >/dev/null 2>&1) ||
  fail "the download does not match its published checksum. Nothing was changed."

ditto -x -k "$download/$zip" "$stage" || fail "could not unpack the app. Nothing was changed."
codesign --verify --deep --strict "$stage/RCP.app" >/dev/null 2>&1 ||
  fail "the unpacked app failed its signature check. Nothing was changed."

if [ -e "$app" ]; then
  mv "$app" "$stage/RCP.previous.app" || fail "could not move the old app aside. Nothing was changed."
  if ! mv "$stage/RCP.app" "$app"; then
    fail "could not install the new app. The old app is restored."
  fi
else
  mv "$stage/RCP.app" "$app" || fail "could not install the new app."
fi

printf 'Installed RCP %s in %s.\n' "$tag" "$app"

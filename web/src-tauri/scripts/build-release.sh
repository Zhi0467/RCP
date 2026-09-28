#!/bin/sh
set -eu

export RCP_DESKTOP_BUILD_KIND=prebuilt

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH= cd -- "$script_dir/../../.." && pwd)

sh "$script_dir/prepare-sidecar.sh"
cd "$repo_root/web"
if [ "${1:-}" = "--updater" ]; then
  # The runtime reads RCP_UPDATE_PUBKEY at compile time; the Tauri signer reads
  # the same key from plugins.updater.pubkey.
  : "${RCP_UPDATE_PUBKEY:?the updater build needs RCP_UPDATE_PUBKEY}"
  : "${RCP_UPDATE_ENDPOINT:?the updater build needs RCP_UPDATE_ENDPOINT}"
  npx tauri build \
    --config src-tauri/tauri.release.conf.json \
    --config src-tauri/tauri.updater.conf.json \
    --config "{\"plugins\":{\"updater\":{\"pubkey\":\"$RCP_UPDATE_PUBKEY\"}}}"
else
  npx tauri build --config src-tauri/tauri.release.conf.json
fi

#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH= cd -- "$script_dir/../../.." && pwd)
target_triple=${RCP_TAURI_TARGET:-aarch64-apple-darwin}

if [ "$target_triple" != "aarch64-apple-darwin" ]; then
  echo "RCP desktop packaging currently supports only aarch64-apple-darwin." >&2
  exit 1
fi

# tauri.release.conf.json copies the one-directory backend this builds into
# the app's Resources/backend.
sh "$repo_root/packaging/build-backend.sh"

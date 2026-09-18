#!/usr/bin/env bash
set -euo pipefail

source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
plugin_id=io.weirdware.themestyles
destination="$HOME/.config/omarchy/plugins/$plugin_id"

omarchy plugin validate "$source_dir"
mkdir -p "$destination"
for filename in manifest.json Panel.qml theme_styles.py agents.py harnesses.py security.py files.py errors.py processes.py storage.py desktop.py policy.xml theme-styles LICENSE; do
  # Copy only changed files to avoid disrupting an open panel on every install.
  if ! cmp -s "$source_dir/$filename" "$destination/$filename"; then
    install -m 644 "$source_dir/$filename" "$destination/$filename"
  fi
done
chmod +x "$destination/theme-styles"
"$destination/theme-styles" migrate
omarchy-shell shell rescanPlugins
omarchy plugin enable "$plugin_id" --section right
printf 'Installed Theme Styles. Open the palette icon in your bar.\n'

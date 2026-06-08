#!/usr/bin/env bash
# Install project git hooks. Run automatically via `npm install` (prepare script).
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"
if [ -z "$REPO_ROOT" ]; then
  echo "install-hooks: not inside a git repo, skipping."
  exit 0
fi

HOOKS_SRC="$REPO_ROOT/scripts/hooks"
HOOKS_DEST="$REPO_ROOT/.git/hooks"

for hook in "$HOOKS_SRC"/*; do
  name="$(basename "$hook")"
  cp "$hook" "$HOOKS_DEST/$name"
  chmod +x "$HOOKS_DEST/$name"
  echo "Installed git hook: $name"
done

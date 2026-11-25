#!/bin/sh
# Install a pre-commit hook that runs harvest/check_publishable.py on the staged contents.
# Bypass for a single commit with `git commit --no-verify` (only after reading the failures).
set -eu

repository_root=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
hooks_directory=$(git -C "$repository_root" rev-parse --git-path hooks)
case "$hooks_directory" in /*) ;; *) hooks_directory="$repository_root/$hooks_directory" ;; esac
hook_path="$hooks_directory/pre-commit"

if [ -e "$hook_path" ] && ! grep -q "check_publishable.py" "$hook_path"; then
    echo "refusing to overwrite existing $hook_path; merge it by hand" >&2
    exit 1
fi

mkdir -p "$hooks_directory"
cat > "$hook_path" <<'HOOK'
#!/bin/sh
exec python3 "$(git rev-parse --show-toplevel)/harvest/check_publishable.py" --staged
HOOK
chmod +x "$hook_path"
echo "installed $hook_path"

#!/bin/sh
# Build the map preview into publish/ and check it with the publish guard. With --push, also force-push publish/ as
# the gh-pages branch of origin (a branch holding only the built site, replaced whole on every deploy).
# Run harvest/build.py first: the data file is map/public/data/map_institutions.json.
set -eu

repository_root=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
staging_directory="$repository_root/publish"
data_file="$repository_root/map/public/data/map_institutions.json"

[ -f "$data_file" ] || { echo "missing $data_file: run harvest/build.py first" >&2; exit 1; }
source_commit=$(git -C "$repository_root" rev-parse --short HEAD)
if [ -n "$(git -C "$repository_root" status --porcelain -- map harvest)" ]; then
    echo "warning: map/ or harvest/ has uncommitted changes; the site will not match commit $source_commit" >&2
fi

(cd "$repository_root/map" && npm run build)
rm -rf "$staging_directory"
cp -R "$repository_root/map/dist" "$staging_directory"
# Pages runs Jekyll by default, which drops files whose names start with "_"; bundle names can.
touch "$staging_directory/.nojekyll"
python3 "$repository_root/harvest/check_publishable.py" --map-site "$staging_directory"

if [ "${1:-}" != "--push" ]; then
    echo "staged and checked $staging_directory; deploy with: sh scripts/publish_map.sh --push"
    exit 0
fi
remote_url=$(git -C "$repository_root" remote get-url origin)
cd "$staging_directory"
git init -q -b gh-pages
git add -A
git commit -q -m "Map site built from $source_commit"
git push --force "$remote_url" gh-pages
rm -rf .git

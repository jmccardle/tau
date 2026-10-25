#!/bin/sh
# Re-render glossary state diagrams whose graph-level label svg_theme.py leaves with literal font attributes.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
arch=$(dirname "$(dirname "$here")")
PY=${PY:-python}
WT=${WT:?set WT to the analysed worktree}
for f in states_serve_attach states_rpc_transport states_extension; do
  t=$(grep -o '<title id="[^"]*">[^<]*' "$arch/glossary/$f.svg" | sed 's/.*">//')
  d=$(grep -o '<desc id="[^"]*">[^<]*' "$arch/glossary/$f.svg" | sed 's/.*">//')
  dot -Tsvg "$arch/glossary/$f.dot" | "$PY" "$WT/scripts/arch/svg_theme.py" - "$here/$f.svg" --title "$t" --desc "$d"
  sed -i 's/ font-family="[^"]*" font-size="[^"]*">/ class="label-soft">/' "$here/$f.svg"
done

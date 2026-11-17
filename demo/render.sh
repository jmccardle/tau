#!/usr/bin/env bash
# Render the races: demo/out/race-{a,b}-{instant,paced}.{mp4,webm,gif}
#
#   demo/render.sh                     all four races at 1280x720
#   RES=1920x1080 demo/render.sh       another size
#   demo/render.sh a-paced b-instant   only these
#
# Needs the installs from demo/setup.sh, vhs, ttyd, ffmpeg and tmux (demo/install-tools.sh).
# Builds the large session of race B the first time (a few minutes).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
export DEMO_WORK="${DEMO_WORK:-${TMPDIR:-/tmp}/tau-demo-$(id -u)}"
RES="${RES:-1280x720}"
GIF_MAX_BYTES="${GIF_MAX_BYTES:-10000000}"
W="${RES%x*}"; H="${RES#*x}"

for t in vhs ttyd ffmpeg tmux; do
  command -v "$t" >/dev/null || { echo "render: $t not found: run demo/install-tools.sh" >&2; exit 1; }
done
[ -x "$DEMO_WORK/venv15/bin/tau" ] && [ -x "$DEMO_WORK/venv16/bin/tau" ] || { echo "render: run demo/setup.sh first" >&2; exit 1; }

cd "$ROOT"
python3 demo/race.py prepare >/dev/null
for v in 15 16; do
  [ -d "$DEMO_WORK/home$v/.tau/sessions.pristine" ] || { python3 demo/race.py build-b; break; }
done

races=("$@")
[ ${#races[@]} -gt 0 ] || races=(a-instant a-paced b-instant b-paced)

mkdir -p demo/out "$DEMO_WORK/tapes"
sed -e "s/^Set Width .*/Set Width $W/" -e "s/^Set Height .*/Set Height $H/" \
    demo/tapes/settings.tape > "$DEMO_WORK/tapes/settings.tape"
if [ "$H" -gt 800 ]; then   # a bigger frame wants a bigger font to stay legible
  sed -i 's/^Set FontSize .*/Set FontSize 24/' "$DEMO_WORK/tapes/settings.tape"
fi

for r in "${races[@]}"; do
  echo "== race-$r ($RES)"
  sed "s|demo/tapes/settings.tape|$DEMO_WORK/tapes/settings.tape|" "demo/tapes/race-$r.tape" \
      > "$DEMO_WORK/tapes/race-$r.tape"
  vhs "$DEMO_WORK/tapes/race-$r.tape"          # writes demo/out/race-$r.mp4 and .webm

  mp4="demo/out/race-$r.mp4"; gif="demo/out/race-$r.gif"
  # Small and crisp: a palette built for this recording, 12 fps, then narrower if still too big.
  for width in "$W" 960 800 640; do
    [ "$width" -le "$W" ] || continue
    ffmpeg -y -loglevel error -i "$mp4" -vf \
      "fps=12,scale=$width:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=128:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle" \
      "$gif"
    [ "$(stat -c %s "$gif")" -le "$GIF_MAX_BYTES" ] && break
  done
done

ls -l demo/out

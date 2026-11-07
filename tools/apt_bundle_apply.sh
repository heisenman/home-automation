#!/usr/bin/env bash
# apt_bundle_apply.sh — install an apt bundle built by tools/ha2_apt_relay.py (copied into the bundle as apply.sh).
# Run ON the target box, as root, by the operator:   sudo ~/apt-bundle-<stamp>/apply.sh
#
#   1. check the bundle's MANIFEST.sha256 (the copy arrived intact);
#   2. hold failover off while packages and services churn: instance/.maintenance-fit, refreshed every 60 s
#      (healthcheck.sh reports FIT while it's < 300 s old; the same inhibit ha2_deploy.sh uses), cleared on ANY exit;
#   3. apt update from the bundle's file: repository into a PRIVATE lists/cache dir (the box's own apt state is
#      untouched): the box's own keyrings verify Debian's InRelease signatures, then Packages, then every .deb;
#   4. apt dist-upgrade from it (apt shows the list and asks Y/n; nothing is downloaded);
#   5. if a newer kernel is installed than the one running (or /run/reboot-required): ASK to reboot.
#      Yes → inhibit cleared, reboot (on ha-2 the VIP moves to the standby and back). No → the active ha-* services
#      are restarted so they pick up the new libraries, and the reboot is left for later.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run as root: sudo $0" >&2; exit 1; }
B="$(cd "$(dirname "$0")" && pwd)"
REPO="${HA_REPO:-/home/visko/home_automation}"
FIT="$REPO/instance/.maintenance-fit"
cd "$B"
echo "== bundle $(basename "$B"): $(wc -l <PACKAGES.txt) package(s)"
sha256sum -c --quiet MANIFEST.sha256 && echo "== manifest OK"

touch_fit() { [ -d "$REPO/instance" ] && touch "$FIT" && chown --reference="$REPO/instance" "$FIT"; }
if [ -d "$REPO/instance" ]; then
  touch_fit
  ( while sleep 60; do touch_fit || true; done ) &
  KEEP=$!
  trap 'kill $KEEP 2>/dev/null || true; rm -f "$FIT"; echo "== failover inhibit cleared"' EXIT
  echo "== failover inhibit set (refreshed every 60 s while this runs)"
fi

sed "s|@BUNDLE@|$B|g" sources.list.in >sources.list
mkdir -p state/lists/partial cache/archives/partial
APT=(-o "Dir::Etc::SourceList=$B/sources.list" -o "Dir::Etc::SourceParts=-" -o "Dir::State::Lists=$B/state/lists"
     -o "Dir::Cache=$B/cache" -o "Acquire::Languages=none" -o "Acquire::By-Hash=no"
     -o "Acquire::IndexTargets::deb::DEP-11::DefaultEnabled=false" -o "APT::Sandbox::User=root")
apt-get "${APT[@]}" update
apt-get "${APT[@]}" dist-upgrade

running="$(uname -r)"
newest="$(ls -1 /boot/vmlinuz-* 2>/dev/null | sed 's|/boot/vmlinuz-||' | sort -V | tail -1)"
if [ -e /run/reboot-required ] || { [ -n "$newest" ] && [ "$newest" != "$running" ]; }; then
  echo
  echo "== reboot needed: running kernel $running, installed $newest"
  [ -f /run/reboot-required.pkgs ] && echo "   (requested by: $(sort -u /run/reboot-required.pkgs | tr '\n' ' '))"
  read -r -p "Reboot now? [y/N] " ans </dev/tty || ans=n
  if [[ "$ans" =~ ^[Yy] ]]; then
    kill "${KEEP:-0}" 2>/dev/null || true; rm -f "$FIT"; trap - EXIT
    echo "== failover inhibit cleared; rebooting"
    systemctl reboot
    exit 0
  fi
  echo "== reboot left for later (the new kernel loads at the next boot)"
fi
mapfile -t units < <(systemctl list-units 'ha-*.service' --state=active --no-legend --plain | awk '{print $1}')
if [ "${#units[@]}" -gt 0 ]; then
  echo "== restarting ${#units[@]} active ha-* service(s) onto the new libraries"
  systemctl restart "${units[@]}"
  sleep 3
  systemctl is-active "${units[@]}" | sort | uniq -c
fi
echo "== done"

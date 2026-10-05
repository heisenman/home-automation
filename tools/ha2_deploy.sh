#!/usr/bin/env bash
# Deploy the files a commit changed to ha-2, behind the VIP inhibit, then restart the given services.
#
#   tools/ha2_deploy.sh <commit> <service> [service...]
#   e.g. tools/ha2_deploy.sh 0bfcd70 ha-controller ha-api ha-api-tls
#
# Run on .210 from the repo checkout. Steps: touch instance/.maintenance-fit on ha-2 (keepalived won't
# flip the VIP while services restart) -> ship every file the commit touched in ONE tar stream ->
# md5-verify every one -> sudo restart the services (passwordless on ha-2) -> confirm active -> clear the
# inhibit. The inhibit is cleared on ANY exit, so a failed run never leaves failover disarmed.
# All ssh calls share one multiplexed connection: over the air-gap WiFi bridge each new ssh handshake
# cost seconds, and the old per-file mkdir+scp loop made a 9-file deploy take minutes.
set -euo pipefail

HA2="${HA2:-192.168.1.200}"
REMOTE_REPO="home_automation"
CTL="$(mktemp -u /tmp/ha2deploy-XXXXXX)"
ssh_ha2() { ssh -o ControlMaster=auto -o ControlPath="$CTL" -o ControlPersist=60 "$HA2" "$@"; }

[ $# -ge 2 ] || { sed -n 2,5p "$0"; exit 2; }
commit="$1"; shift
services=("$@")

cd "$(git rev-parse --show-toplevel)"
git rev-parse --verify --quiet "$commit^{commit}" >/dev/null \
  || { echo "ABORT: '$commit' is not a commit here (typo?). Recent commits:"; git log --oneline -5; exit 1; }
mapfile -t files < <(git diff-tree --no-commit-id --name-only -r --diff-filter=AM "$commit")
[ ${#files[@]} -gt 0 ] || { echo "no added/modified files in $commit"; exit 1; }
for f in "${files[@]}"; do
  git diff --quiet "$commit" -- "$f" || { echo "ABORT: $f differs from $commit in this checkout"; exit 1; }
done

echo "== deploying $(git log --oneline -1 "$commit") to $HA2"
printf '   %s\n' "${files[@]}"

ssh_ha2 "touch ~/$REMOTE_REPO/instance/.maintenance-fit"
trap 'ssh_ha2 "rm -f ~/$REMOTE_REPO/instance/.maintenance-fit" && echo "== VIP inhibit cleared"; ssh -o ControlPath="$CTL" -O exit "$HA2" 2>/dev/null' EXIT
echo "== VIP inhibit set"

tar cf - "${files[@]}" | ssh_ha2 "cd ~/$REMOTE_REPO && tar xf -"
md5sum "${files[@]}" | ssh_ha2 "cd ~/$REMOTE_REPO && md5sum -c --quiet" && echo "== md5 verified (${#files[@]} files)"

echo "== restarting: ${services[*]}"
ssh_ha2 "sudo -n systemctl restart ${services[*]} && sleep 3 && systemctl is-active ${services[*]}" \
  && echo "== DONE — all services active"

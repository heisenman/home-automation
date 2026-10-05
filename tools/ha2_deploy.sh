#!/usr/bin/env bash
# Deploy the files a commit changed to ha-2, behind the VIP inhibit, then restart the given services.
#
#   tools/ha2_deploy.sh <commit> <service> [service...]
#   e.g. tools/ha2_deploy.sh 0bfcd70 ha-controller ha-api ha-api-tls
#
# Run on .210 from the repo checkout. Steps: touch instance/.maintenance-fit on ha-2 (keepalived won't
# flip the VIP while services restart) -> scp each file the commit touched -> md5-verify every one ->
# sudo restart the services (asks for ha-2's sudo password) -> confirm active -> clear the inhibit.
# The inhibit is cleared on ANY exit, so a failed run never leaves failover disarmed.
set -euo pipefail

HA2="${HA2:-192.168.1.200}"
REMOTE_REPO="home_automation"

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

ssh "$HA2" "touch ~/$REMOTE_REPO/instance/.maintenance-fit"
trap 'ssh "$HA2" "rm -f ~/$REMOTE_REPO/instance/.maintenance-fit" && echo "== VIP inhibit cleared"' EXIT
echo "== VIP inhibit set"

for f in "${files[@]}"; do
  ssh "$HA2" "mkdir -p ~/$REMOTE_REPO/$(dirname "$f")"
  scp -q "$f" "$HA2:$REMOTE_REPO/$f"
done
md5sum "${files[@]}" | ssh "$HA2" "cd ~/$REMOTE_REPO && md5sum -c --quiet" && echo "== md5 verified (${#files[@]} files)"

echo "== restarting: ${services[*]} (sudo password for ha-2 may be asked)"
ssh -t "$HA2" "sudo systemctl restart ${services[*]}"
sleep 5
ssh "$HA2" "systemctl is-active ${services[*]}" && echo "== DONE — all services active"

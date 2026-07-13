#!/bin/bash
# ota_edge_node.sh <node_id> <board_subdir> [fw_tag]
# Build a node-branded edge image (from its manifest identity) and OTA it from ha-2 — the proven fleet recipe,
# generalized across boards (esp32c6 | esp32s3-eth). Node-side gate is IDENTITY-ONLY (ha_ota.c) so re-OTA of
# the same tag is fine.  Steps: re-emit secrets.h (reuse the node's HMAC secret) -> brand version.txt
# (<node>@<fw>) -> reconfigure+build -> scp to ha-2 ~/ota-canary -> signed edge_ota (serve .1.210, broker .1.200).
set -euo pipefail
NODE="${1:?usage: ota_edge_node.sh <node_id> <board_subdir> [fw_tag]}"
BOARD="${2:?board_subdir, e.g. esp32c6 or esp32s3-eth}"
FW="${3:-v20-battfix}"
REPO=/home/visko/home_automation
BDIR="$REPO/edge/$BOARD"
cd "$REPO"
LOG=/tmp/claude-1000/-home-visko-home-automation/3b184822-e6c9-46ee-b2b3-27c0f4cdd711/scratchpad/ota-${NODE}.log

echo "[$NODE] emit secrets + brand ${NODE}@${FW}"
HA_MASTER_PASSPHRASE="$(cat instance/.master_pass)" venv/bin/python3 tools/enroll_node.py \
  --node-id "$NODE" --from-manifest --reuse --out "$BDIR/main/secrets.h" >/dev/null 2>&1
printf '%s@%s' "$NODE" "$FW" > "$BDIR/version.txt"

echo "[$NODE] build (log -> $LOG)"
( cd "$BDIR" && source ~/esp/esp-idf/export.sh >/dev/null 2>&1 \
  && idf.py reconfigure >/dev/null 2>&1 && idf.py build ) > "$LOG" 2>&1
BIN=$(ls "$BDIR"/build/ha-edge-*.bin | grep -vE 'bootloader|partition|ota_data' | head -1)
grep -qa "${NODE}@${FW}" "$BIN" || { echo "[$NODE] BRAND VERIFY FAIL"; exit 1; }

echo "[$NODE] scp -> ha-2"
scp -i ~/.ssh/id_cluster -o ConnectTimeout=8 "$BIN" "visko@192.168.1.210:~/ota-canary/${NODE}-${FW}.bin" >/dev/null 2>&1
L=$(md5sum "$BIN" | awk '{print $1}')
R=$(ssh -i ~/.ssh/id_cluster -o ConnectTimeout=8 visko@192.168.1.210 "md5sum ~/ota-canary/${NODE}-${FW}.bin | awk '{print \$1}'")
[ "$L" = "$R" ] || { echo "[$NODE] MD5 MISMATCH"; exit 1; }

echo "[$NODE] OTA from ha-2 (serve .1.210, broker .1.200)"
SECRET=$(grep -oP 'HA_CMD_SECRET\s+"\K[^"]+' "$BDIR/main/secrets.h")
ssh -i ~/.ssh/id_cluster -o ConnectTimeout=8 visko@192.168.1.210 \
  "HA_CMD_SECRET='$SECRET' ~/ota-venv/bin/python3 ~/home_automation/tools/edge_ota.py \
     --node $NODE --bin ~/ota-canary/${NODE}-${FW}.bin --serve-ip 192.168.1.210 --broker 192.168.1.200"

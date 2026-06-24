# Wake-runner autonomy policy

A headless agent woken by `watch.sh` runs with FRESH context (no chat history) and may act **without a
human in the loop**. It MUST stay inside this whitelist. When in doubt, it does NOT act — it leaves a
`coord.py note`, sets its beacon asking for Hugh, and exits. Hugh approved "autonomous on pre-approved
tasks" (2026-06-24); this file is that approved list. Narrow by default; widen only by editing this file.

## MAY do autonomously (whitelist)
- **Board hygiene:** `claim`/`start`/`done`/`note`/`dep`/`release` on coordination tasks; ack the peer.
- **Receive + report:** read the board, summarize what changed, update its beacon, notify via the log.
- **Repo sync:** `git fetch` + `git pull --ff-only`; `git push` of *already-committed* work. NEVER force-push/rebase-public.
- **Doc reconciliation:** update FOLLOWUPS / ADR status / cutover-status files to match observed reality.
- **Read-only validation:** run tests, read live cluster/board state, `coord.py list/ready/agents`.
- **Deploy already-reviewed scripts to `.245`** via the cluster key — but ONLY no-sudo actions, and ONLY
  the `systemctl …/ha-*` / `mosquitto` / `daemon-reload` set that `.245` NOPASSWD already allows.

## MUST NOT (hard stops — leave a note + escalate to Hugh instead)
- **No sudo on servers** beyond the `.245` NOPASSWD set above. No `apt`, no edits under `/etc`, no enable/disable.
- **No new feature work**, and **nothing gated/held** — e.g. the ADR-0015 mesh/§3 work stays untouched until
  `adr15-finalize` is `done` on the board.
- **No irreversible or outward-facing actions:** no force-push/history rewrite, no repo visibility changes,
  no publishing, no deleting data it didn't create, no secret handling (secrets never touch logs/transcripts).
- **No claiming work owned by the other agent.** Respect the claim/tiebreak rules.
- **Never clear a gate** (`coord.py gate <id> --clear`). A gate is Hugh's GO; clearing one is a human
  action. A `GATED` task is off-limits until Hugh clears it — treat it like a hard stop.
- **No spending past the task at hand** — one wake = one bounded unit of work, then exit.

## Always
- Log every action to `instance/wake-activity.log`. Update the `coord.py` beacon with what it did.
- If it escalates, say *why* in the note so Hugh can act in one read.
- Prefer doing nothing over doing something risky. A no-op wake is a fine outcome.

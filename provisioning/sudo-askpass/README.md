# sudo-hardening + ASKPASS (2026-07-10)

`.210` dropped the broad `visko NOPASSWD: ALL` grant (only `systemctl ha-*`/`mosquitto` stay NOPASSWD;
general sudo requires **root's** password via `Defaults:visko rootpw`). That broke bare `sudo` for
headless/automated callers (wake runners, timers) — this ASKPASS wiring restores them without un-hardening.

Pieces (install order):
1. `ha-sudo-askpass` -> `/usr/local/sbin/ha-sudo-askpass` (0555) — prints the password from
   `instance/.rootpwd` (the secret lives only there, 0600).
2. `sudo-wrapper` -> `/usr/local/bin/sudo` (0755) — thin PATH wrapper (before `/usr/bin`) that injects `-A`
   for bare `sudo`, so headless callers use the askpass. Passes through untouched when the caller already
   manages auth (`-A`/`-S`/`-n`/`-e`/help/version). Real sudo at `/usr/bin/sudo` (no recursion).
3. `/etc/sudo.conf`: `Path askpass /usr/local/sbin/ha-sudo-askpass` (belt-and-suspenders for `-A`).

Net: bare `sudo <cmd>` works for humans AND automation (password auto-supplied from `.rootpwd`);
`sudo -S < instance/.rootpwd` still works; `systemctl ha-*` stays NOPASSWD. Security posture: escalation
requires reading `.rootpwd` (auditable + revocable) rather than a blanket NOPASSWD.

// A modal pick-one menu (Catherine, 2026-10-30): a title, one large button per option, and Cancel, over a
// dimmed backdrop on the top layer (so a rebuild of the screen underneath doesn't touch it). Tapping an
// option closes the menu and calls `cb(index)`; Cancel or a tap on the backdrop just closes it. First use:
// the Sensors grid's Other column, to choose which of a sensor's extra readings to graph.
#pragma once

#define UI_PICK_MAX 8      // options shown (more are dropped, with a log line)

// Open the menu. `labels` are copied, so they may be freed once this returns. Opening while a menu is
// already open replaces it. Caller holds the LVGL lock.
void ui_pick_open(const char *title, const char *const *labels, int n, void (*cb)(int index));

// Close it without choosing (no callback). Safe when nothing is open. Caller holds the LVGL lock.
void ui_pick_close(void);

// Spike 0 (roadmap #5, ADR-0019 Phase 6): prove the D1001 P4 NimBLE host can act as a GATT CENTRAL over
// the C6 esp_hosted HCI link — connect to a SwitchBot + discover its services — not merely observe
// adverts. Minimal + self-contained: no SwitchBot history protocol yet, just connect → discover →
// report → disconnect. If this works, the full gatt_history.c port is "adapt two deps + lift". Reuses the
// exact connect pattern from edge/esp32c6/main/gatt_history.c so the spike matches the proven path.
#pragma once
#include <stdbool.h>

// Connect to `mac_str` (must already be cached by the passive scanner — i.e. heard advertising), discover
// all services, report the outcome via the reporter, then disconnect + resume scanning. One at a time.
// Returns false if busy / the addr isn't cached / the connect call is rejected.
bool gatt_probe_start(const char *mac_str);

// Register a line sink (the composition root wires this to an MQTT publish on d1001-beachhead/gatt) so the
// probe's progress is visible remotely. NULL-safe until set.
void gatt_probe_set_reporter(void (*fn)(const char *line));

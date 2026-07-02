#pragma once
// Compatibility shim (ADR-0020 Stage-2 migration): the observer moved to the shared
// firmware/components/ha_ble_scan component — including the transport-aware duty-cycle
// this node originated (now the cfg.shared_radio flag). Consumers (gatt_*, ha_ota,
// app_main) keep including "ble_scan.h" unchanged; this forwards to the shared header.
#include "ha_ble_scan.h"

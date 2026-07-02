#pragma once
// Compatibility shim (ADR-0020 Stage-2 migration): the observer moved to the shared
// firmware/components/ha_ble_scan component. Consumers (gatt_*, ha_ota, app_main) keep
// including "ble_scan.h" unchanged so they stay byte-identical to the other forks; this
// just forwards to the shared header. Retire once all nodes migrate.
#include "ha_ble_scan.h"

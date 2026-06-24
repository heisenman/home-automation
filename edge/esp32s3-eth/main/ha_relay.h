#pragma once
#include <stdbool.h>
// ADR-0015 Phase B (Tier 2): the dictator tells this node which meters it's the PREFERRED relay for, so
// the node stops wasting radio/CPU forwarding adverts a closer source already delivers. A signed, retained
// `relay_assign` directive on home/edge/<node>/relay carries an allowlist of MACs + a monotonic epoch.
//
// Default before any directive = **relay-all** (today's behavior — an un-provisioned node is useful
// immediately; the dictator only ever *narrows* it). An explicit empty allowlist = relay nothing.
// The allowlist + epoch persist in NVS (survive reboot) and the directive is retained (re-read on connect).

void ha_relay_init(void);                  // load persisted allowlist + epoch from NVS (call once, early)
bool ha_relay_allowed(const char *mac_str); // should this MAC's adverts be relayed? (true = relay-all default)
void ha_relay_apply(const char *json);      // apply a VERIFIED relay_assign directive (epoch-guarded, persists)

// Host test for the ha_partmig planner. Tables are built entry-by-entry in the on-flash format
// (esp_partition_info_t: magic 0x50AA, type, subtype, offset, size, label[16], flags), the Tasmota one from
// the 4 MB safeboot layout (nvs 0x9000, otadata 0xE000, safeboot 0x10000, app0 0xE0000, spiffs 0x2B0000).
#include <assert.h>
#include <stdio.h>
#include <string.h>

#include "ha_partmig.h"

static int add(uint8_t *t, int i, uint8_t type, uint8_t sub, uint32_t off, uint32_t size, const char *label) {
    uint8_t *e = t + i * 32;
    memset(e, 0, 32);
    e[0] = 0xAA; e[1] = 0x50; e[2] = type; e[3] = sub;
    for (int b = 0; b < 4; b++) { e[4 + b] = off >> (8 * b); e[8 + b] = size >> (8 * b); }
    strncpy((char *)e + 12, label, 16);
    return i + 1;
}

static size_t tasmota(uint8_t *t) {
    memset(t, 0xFF, HA_PARTMIG_TABLE_MAX);
    int i = 0;
    i = add(t, i, 1, 0x02, 0x9000, 0x5000, "nvs");
    i = add(t, i, 1, 0x00, 0xE000, 0x2000, "otadata");
    i = add(t, i, 0, 0x00, 0x10000, 0xD0000, "safeboot");
    i = add(t, i, 0, 0x10, 0xE0000, 0x1D0000, "app0");
    i = add(t, i, 1, 0x82, 0x2B0000, 0x150000, "spiffs");
    memset(t + i * 32, 0xEB, 2);                       // an MD5 entry, which the parser must skip
    return HA_PARTMIG_TABLE_MAX;
}

static size_t ours(uint8_t *t, uint32_t ota0) {
    memset(t, 0xFF, HA_PARTMIG_TABLE_MAX);
    int i = 0;
    i = add(t, i, 1, 0x02, 0x9000, 0x5000, "nvs");
    i = add(t, i, 1, 0x00, 0xE000, 0x2000, "otadata");
    i = add(t, i, 1, 0x40, 0x10000, 0xD0000, "spare");
    i = add(t, i, 0, 0x10, ota0, 0x190000, "ota_0");
    i = add(t, i, 0, 0x11, 0x270000, 0x190000, "ota_1");
    return (size_t)(i * 32);
}

int main(void) {
    uint8_t cur[HA_PARTMIG_TABLE_MAX], o[HA_PARTMIG_TABLE_MAX];
    char why[160];
    size_t ol = ours(o, 0xE0000);

    ha_partmig_entry_t e[24];
    assert(ha_partmig_parse(cur, tasmota(cur), e, 24) == 5);
    assert(strcmp(e[2].label, "safeboot") == 0 && e[3].offset == 0xE0000 && e[3].size == 0x1D0000);

    // the case we exist for: Tasmota table, running from its app0
    assert(ha_partmig_plan(cur, sizeof cur, o, ol, 0xE0000, 1100000, why, sizeof why) == HA_PARTMIG_MIGRATE);

    // running from Tasmota's SAFEBOOT (0x10000) instead: our ota_0 would not hold the running image
    assert(ha_partmig_plan(cur, sizeof cur, o, ol, 0x10000, 1100000, why, sizeof why) == HA_PARTMIG_FOREIGN_UNSAFE);

    // image too big for our ota_0
    assert(ha_partmig_plan(cur, sizeof cur, o, ol, 0xE0000, 0x190001, why, sizeof why) == HA_PARTMIG_FOREIGN_UNSAFE);

    // our ota_0 placed elsewhere (a build error that would orphan the running image)
    uint8_t bad[HA_PARTMIG_TABLE_MAX];
    size_t bl = ours(bad, 0x10000);
    assert(ha_partmig_plan(cur, sizeof cur, bad, bl, 0xE0000, 1100000, why, sizeof why) == HA_PARTMIG_FOREIGN_UNSAFE);

    // already migrated: the live table IS ours -> nothing to do (the steady state on every later boot)
    uint8_t live[HA_PARTMIG_TABLE_MAX];
    memset(live, 0xFF, sizeof live);
    memcpy(live, o, ol);
    assert(ha_partmig_plan(live, sizeof live, o, ol, 0xE0000, 1100000, why, sizeof why) == HA_PARTMIG_NONE);

    // a foreign table without safeboot (e.g. a stock ESPHome layout) is left alone
    uint8_t esph[HA_PARTMIG_TABLE_MAX];
    memset(esph, 0xFF, sizeof esph);
    int i = 0;
    i = add(esph, i, 1, 0x00, 0x9000, 0x2000, "otadata");
    i = add(esph, i, 0, 0x10, 0x10000, 0x1C0000, "app0");
    assert(ha_partmig_plan(esph, sizeof esph, o, ol, 0x10000, 1100000, why, sizeof why) == HA_PARTMIG_FOREIGN_UNSAFE);

    // our own table malformed
    uint8_t junk[64];
    memset(junk, 0xFF, sizeof junk);
    assert(ha_partmig_plan(cur, sizeof cur, junk, sizeof junk, 0xE0000, 1100000, why, sizeof why) == HA_PARTMIG_BAD_INPUT);

    puts("ha_partmig: all tests passed");
    return 0;
}

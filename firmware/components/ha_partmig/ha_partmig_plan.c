// ha_partmig planner — pure C, host-tested. See include/ha_partmig.h.
#include "ha_partmig.h"

#include <stdio.h>
#include <string.h>

#define ENTRY 32
#define MAGIC_PART 0x50AA
#define TYPE_APP   0x00
#define TYPE_DATA  0x01
#define SUB_OTA0   0x10
#define SUB_OTADATA 0x00
#define SUB_NVS    0x02

static uint32_t le32(const uint8_t *p) { return p[0] | (p[1] << 8) | (p[2] << 16) | ((uint32_t)p[3] << 24); }

int ha_partmig_parse(const uint8_t *raw, size_t len, ha_partmig_entry_t *out, int max) {
    int n = 0;
    for (size_t o = 0; o + ENTRY <= len && n < max; o += ENTRY) {
        const uint8_t *e = raw + o;
        uint16_t magic = e[0] | (e[1] << 8);
        if (magic == 0xEBEB) continue;                       // MD5 entry
        if (magic != MAGIC_PART) break;
        out[n].type = e[2];
        out[n].subtype = e[3];
        out[n].offset = le32(e + 4);
        out[n].size = le32(e + 8);
        memcpy(out[n].label, e + 12, 16);
        out[n].label[16] = '\0';
        n++;
    }
    return n;
}

static const ha_partmig_entry_t *find(const ha_partmig_entry_t *t, int n, int type, int sub, uint32_t off,
                                      const char *label) {
    for (int i = 0; i < n; i++) {
        if (type >= 0 && t[i].type != type) continue;
        if (sub >= 0 && t[i].subtype != sub) continue;
        if (off != UINT32_MAX && t[i].offset != off) continue;
        if (label && strncmp(t[i].label, label, 16) != 0) continue;
        return &t[i];
    }
    return NULL;
}

#define WHY(...) do { if (why && why_sz) snprintf(why, why_sz, __VA_ARGS__); } while (0)

ha_partmig_verdict_t ha_partmig_plan(const uint8_t *cur, size_t cur_len, const uint8_t *ours, size_t ours_len,
                                     uint32_t running_addr, uint32_t image_len, char *why, size_t why_sz) {
    ha_partmig_entry_t c[24], o[24];
    int nc = ha_partmig_parse(cur, cur_len, c, 24), no = ha_partmig_parse(ours, ours_len, o, 24);
    if (no == 0 || ours_len > HA_PARTMIG_TABLE_MAX) { WHY("embedded table unparseable"); return HA_PARTMIG_BAD_INPUT; }

    const ha_partmig_entry_t *ota0 = find(o, no, TYPE_APP, SUB_OTA0, UINT32_MAX, NULL);
    if (!ota0 || !find(o, no, TYPE_DATA, SUB_NVS, UINT32_MAX, NULL) ||
        !find(o, no, TYPE_DATA, SUB_OTADATA, UINT32_MAX, NULL)) {
        WHY("embedded table lacks ota_0/nvs/otadata");
        return HA_PARTMIG_BAD_INPUT;
    }
    if (cur_len >= ours_len && memcmp(cur, ours, ours_len) == 0) { WHY("table already ours"); return HA_PARTMIG_NONE; }
    if (!find(c, nc, TYPE_APP, -1, UINT32_MAX, "safeboot")) {
        WHY("foreign table without a 'safeboot' partition (%d entries) — not a layout we take in place", nc);
        return nc ? HA_PARTMIG_FOREIGN_UNSAFE : HA_PARTMIG_NONE;
    }
    if (!find(c, nc, TYPE_APP, -1, running_addr, NULL)) {
        WHY("running at 0x%lx, which is not an app slot of the current table", (unsigned long)running_addr);
        return HA_PARTMIG_FOREIGN_UNSAFE;
    }
    if (ota0->offset != running_addr) {
        WHY("our ota_0 is at 0x%lx but we run from 0x%lx — would orphan the running image",
            (unsigned long)ota0->offset, (unsigned long)running_addr);
        return HA_PARTMIG_FOREIGN_UNSAFE;
    }
    if (image_len == 0 || image_len > ota0->size) {
        WHY("image (%lu B) does not fit our ota_0 (%lu B)", (unsigned long)image_len, (unsigned long)ota0->size);
        return HA_PARTMIG_FOREIGN_UNSAFE;
    }
    WHY("Tasmota safeboot layout, running from its app slot 0x%lx — migrating to our A/B table",
        (unsigned long)running_addr);
    return HA_PARTMIG_MIGRATE;
}

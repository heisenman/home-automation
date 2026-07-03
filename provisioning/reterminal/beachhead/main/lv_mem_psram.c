// Custom LVGL allocator core → PSRAM (ADR-0020 / graph-memory arms-race fix).
//
// LVGL 9.5's default builtin allocator lives in a fixed static internal-RAM pool
// (CONFIG_LV_MEM_SIZE_KILOBYTES, LV_MEM_ADR=0). That 64KB ceiling is what crashed stacked chart
// expansions (transient draw-layer buffers exhaust the pool → NULL → store/illegal fault), and
// growing it boot-loops the panel because the static pool eats the internal RAM the MIPI-DSI
// bring-up needs. LVGL 9 also removed lv_chart_set_ext_y_array, so chart point arrays can't be
// individually relocated — the whole LVGL heap has to move.
//
// Selecting LV_USE_CUSTOM_MALLOC drops the builtin pool entirely and routes every lv_malloc()
// here. We serve from PSRAM (32MB, 200MHz octal, already home to the framebuffer) and fall back
// to internal only if PSRAM is somehow exhausted. Net: LVGL memory is effectively unbounded for
// this UI, AND internal RAM is *freed* (opposite of the 128KB bump that broke boot). LVGL data is
// CPU-only (objects/styles/draw layers) — never DMA'd — so non-DMA PSRAM is safe.
//
// NB: this is the correctness fix, not the performance-tuned end state. A later refactor should
// inventory what LVGL allocates vs. what else lives in PSRAM and rebalance hot allocations back
// into fast internal RAM (Hugh's action item).
#include "sdkconfig.h"
#include "lvgl.h"
// Guard on the Kconfig switch itself (globally defined via sdkconfig.h), not LVGL's internal
// LV_USE_STDLIB_MALLOC macro — the latter doesn't resolve to LV_STDLIB_CUSTOM in this TU's
// include order, which silently compiled the file to nothing (undefined lv_*_core at link).
#ifdef CONFIG_LV_USE_CUSTOM_MALLOC
#include <string.h>
#include "esp_heap_caps.h"

// PSRAM first, internal RAM as a safety net (num=2 cap masks, tried in order).
#define UI_LV_CAPS 2, (MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT), (MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT)

void lv_mem_init(void) { return; }      // PSRAM is up at boot (SPIRAM_BOOT_INIT); nothing to do
void lv_mem_deinit(void) { return; }

lv_mem_pool_t lv_mem_add_pool(void *mem, size_t bytes)
{
    LV_UNUSED(mem); LV_UNUSED(bytes);
    return NULL;   // not supported: the ESP heap allocator manages the pool
}
void lv_mem_remove_pool(lv_mem_pool_t pool) { LV_UNUSED(pool); return; }

void *lv_malloc_core(size_t size)
{
    return heap_caps_malloc_prefer(size, UI_LV_CAPS);
}

void *lv_realloc_core(void *p, size_t new_size)
{
    return heap_caps_realloc_prefer(p, new_size, UI_LV_CAPS);
}

void lv_free_core(void *p)
{
    heap_caps_free(p);
}

void lv_mem_monitor_core(lv_mem_monitor_t *mon_p)
{
    if (mon_p) memset(mon_p, 0, sizeof(*mon_p));   // ESP heap tracks its own stats; not reported here
}

lv_result_t lv_mem_test_core(void)
{
    return LV_RESULT_OK;
}

#endif /*CONFIG_LV_USE_CUSTOM_MALLOC*/

/*
 * ADR-0030 Verification B1 — 2nd-stage bootloader OVERRIDE for button-held recovery-to-golden.
 *
 * Based VERBATIM on the ESP-IDF example
 *   examples/custom_bootloader/bootloader_override/bootloader_components/main/bootloader_start.c
 * (IDF v5.4). The ONLY functional change is inside select_partition_number(): if the recovery
 * button (GPIO3) is held HIGH continuously for RECOVERY_HOLD_SEC seconds, force-boot the golden
 * recovery image (ota_1) instead of the otadata-selected partition.
 *
 * Why the bootloader (not just the app A2 gate): only the 2nd-stage bootloader runs before a
 * crashing app. This covers the "bad/crash boot -> long-press -> golden" case the app cannot.
 *
 * SECURE BOOT IS OFF on this project (verified), so this custom bootloader needs no signing and a
 * bad flash is recoverable via ROM download-mode + JTAG.
 *
 * Recovery semantics: this forces the boot INDEX only while the button is held; it never writes
 * otadata. So a normal (released) boot is untouched, and releasing + rebooting returns to the
 * otadata-selected app automatically — the recovery is inherently transient, never sticky.
 *
 * The long-hold helper enables the internal pull-up and returns immediately when the pin is NOT
 * at the target level, so a released boot (GPIO3 low) pays ZERO delay; only an actual high-hold
 * blocks (up to RECOVERY_HOLD_SEC). Polarity is VERIFIED active-HIGH (held=1); see recovery.h.
 */
#include <stdbool.h>
#include "sdkconfig.h"
#include "esp_log.h"
#include "bootloader_init.h"
#include "bootloader_utility.h"
#include "bootloader_common.h"
#include "recovery.h"          // single source of truth: RECOVERY_BUTTON_GPIO, RECOVERY_BUTTON_ACTIVE_LVL

// Deliberate long-press so a transient touch never diverts a good boot. Confirmed with Hugh 2026-07-08.
#define RECOVERY_HOLD_SEC   3
// Golden is the IMMUTABLE `test`-subtype partition (partitions.csv) — never an ota_N slot, so a normal
// OTA can't clobber it. The bootloader boots it explicitly by TEST_APP_INDEX (see bootloader_utility.c
// load_boot_image: start_index==TEST_APP_INDEX loads bs->test directly).

static const char* TAG = "boot";

static int select_partition_number(bootloader_state_t *bs);

/*
 * We arrive here after the ROM bootloader finished loading this second stage bootloader from flash.
 * The hardware is mostly uninitialized, flash cache is down and the app CPU is in reset.
 * We do have a stack, so we can do the initialization in C.
 */
void __attribute__((noreturn)) call_start_cpu0(void)
{
    // 1. Hardware initialization
    if (bootloader_init() != ESP_OK) {
        bootloader_reset();
    }

#ifdef CONFIG_BOOTLOADER_SKIP_VALIDATE_IN_DEEP_SLEEP
    // If this boot is a wake up from the deep sleep then go to the short way,
    // try to load the application which worked before deep sleep.
    // It skips a lot of checks due to it was done before (while first boot).
    bootloader_utility_load_boot_image_from_deep_sleep();
    // If it is not successful try to load an application as usual.
#endif

    // 2. Select the number of boot partition
    bootloader_state_t bs = {0};
    int boot_index = select_partition_number(&bs);
    if (boot_index == INVALID_INDEX) {
        bootloader_reset();
    }

    // 3. Load the app image for booting
    bootloader_utility_load_boot_image(&bs, boot_index);
}

// Select the number of boot partition
static int select_partition_number(bootloader_state_t *bs)
{
    // 1. Load partition table
    if (!bootloader_utility_load_partition_table(bs)) {
        ESP_LOGE(TAG, "load partition table error!");
        return INVALID_INDEX;
    }

    // 2. ADR-0030 recovery: GPIO3 held HIGH for RECOVERY_HOLD_SEC -> force the immutable golden
    //    (test) partition. Released pin returns immediately (no normal-boot delay). Guarded on the
    //    test partition actually existing in the table.
    if (bs->test.offset != 0 &&
        bootloader_common_check_long_hold_gpio_level(
            RECOVERY_BUTTON_GPIO, RECOVERY_HOLD_SEC, RECOVERY_BUTTON_ACTIVE_LVL) == GPIO_LONG_HOLD) {
        esp_rom_printf("[%s] ADR-0030 RECOVERY: gpio%d held %ds -> booting GOLDEN (test @0x%x)\n",
                       TAG, RECOVERY_BUTTON_GPIO, RECOVERY_HOLD_SEC, bs->test.offset);
        return TEST_APP_INDEX;
    }

    // 3. Otherwise, the normal otadata-selected boot partition.
    return bootloader_utility_get_selected_boot_partition(bs);
}

// Return global reent struct if any newlib functions are linked to bootloader
struct _reent *__getreent(void)
{
    return _GLOBAL_REENT;
}

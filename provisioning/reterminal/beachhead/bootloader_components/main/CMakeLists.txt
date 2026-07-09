# ADR-0030 B1: full 2nd-stage bootloader override (naming it `main` replaces the stock bootloader main).
# Verbatim from the IDF custom_bootloader/bootloader_override example, plus INCLUDE_DIRS to the app's
# main/ so the recovery button pin + polarity are single-sourced from recovery.h.
idf_component_register(SRCS "bootloader_start.c"
                    INCLUDE_DIRS "${CMAKE_CURRENT_LIST_DIR}/../../main"
                    REQUIRES bootloader bootloader_support)

idf_build_get_property(target IDF_TARGET)

set(target_folder "${target}")

# Use the linker script files from the actual bootloader
set(scripts "${IDF_PATH}/components/bootloader/subproject/main/ld/${target_folder}/bootloader.ld"
            "${IDF_PATH}/components/bootloader/subproject/main/ld/${target_folder}/bootloader.rom.ld")

target_linker_script(${COMPONENT_LIB} INTERFACE "${scripts}")

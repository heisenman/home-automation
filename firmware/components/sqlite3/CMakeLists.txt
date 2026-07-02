# Vendored SQLite amalgamation (3.45.1) + a minimal FATFS-backed VFS.
# ADR-0022 / ha_replica: the panel reads a compact rung DB off the SD card.
# Registry-free (air-gap clean), mirrors the repo's vendored-driver pattern.
idf_component_register(
    SRCS "sqlite3.c" "esp_fatfs_vfs.c"
    INCLUDE_DIRS "include"
    PRIV_REQUIRES fatfs vfs esp_hw_support freertos)

# Lean, SD-friendly build. OS_OTHER=1 => our esp_fatfs_vfs owns sqlite3_os_init.
# THREADSAFE=0: callers must serialize access (single spike/replica task).
target_compile_definitions(${COMPONENT_LIB} PRIVATE
    NDEBUG=1                     # IDF keeps asserts on; SQLite's internal debug asserts
                                 # reference SQLITE_DEBUG-only symbols -> strip them here only
    SQLITE_OS_OTHER=1
    SQLITE_THREADSAFE=0
    SQLITE_TEMP_STORE=3          # temp tables/indices in RAM: no temp-file VFS needed
    SQLITE_OMIT_WAL=1            # no WAL on FATFS; rollback/OFF journalling only
    SQLITE_OMIT_LOAD_EXTENSION=1
    SQLITE_DQS=0
    SQLITE_DEFAULT_MEMSTATUS=0
    SQLITE_DEFAULT_PAGE_SIZE=4096
    SQLITE_MAX_MMAP_SIZE=0       # no mmap over FATFS
    SQLITE_DEFAULT_SYNCHRONOUS=1)

# The amalgamation is warning-noisy; keep it quiet and size-optimised.
# The -include shim neutralises IDF's argument-evaluating assert under NDEBUG
# (CONFIG_COMPILER_ASSERT_NDEBUG_EVALUATE=y) so SQLite's release build compiles.
set_source_files_properties(sqlite3.c PROPERTIES COMPILE_OPTIONS
    "-w;-Os;-include;${CMAKE_CURRENT_SOURCE_DIR}/sqlite_assert_shim.h")

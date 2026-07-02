# Generic SD/FAT file-ops over MQTT (ADR-0020). Device-agnostic: it drives whatever
# filesystem is mounted at /sdcard via POSIX + esp_vfs_fat_info, so any node with an SD
# card and an MQTT publish sink reuses it unchanged. json = cJSON, mbedtls = base64.
idf_component_register(SRCS "fs_ops.c"
                       INCLUDE_DIRS "include"
                       REQUIRES fatfs json mbedtls)

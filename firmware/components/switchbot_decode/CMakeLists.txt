# Pure decoder — no ESP/Bluetooth deps, so no REQUIRES. Public header in include/.
idf_component_register(SRCS "switchbot_decode.c"
                       INCLUDE_DIRS "include")

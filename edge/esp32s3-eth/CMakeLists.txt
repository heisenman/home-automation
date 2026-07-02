# Top-level ESP-IDF project file.
cmake_minimum_required(VERSION 3.16)
# Shared firmware components (ADR-0020) live at repo-root firmware/components.
set(EXTRA_COMPONENT_DIRS "${CMAKE_CURRENT_LIST_DIR}/../../firmware/components")
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(ha-edge-s3-eth)   # was ha-edge-c6 (fork leftover); bin is now build/ha-edge-s3-eth.bin

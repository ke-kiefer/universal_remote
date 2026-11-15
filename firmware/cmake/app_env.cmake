# Reads firmware/.env and generates app_env.h with APP_* defines.
#
# All configuration (WLAN, server, camera settings) lives in firmware/.env, never in source.
# CMake re-runs automatically when .env changes.

# Values may contain characters that have special meaning in CMake lists.
set(_SEMI "@@SEMI@@")
set(_LBR "@@LBR@@")
set(_RBR "@@RBR@@")

function(_app_env_escape_c in out)
    string(REPLACE "\\" "\\\\" v "${in}")
    string(REPLACE "\"" "\\\"" v "${v}")
    string(REPLACE "${_SEMI}" ";" v "${v}")
    string(REPLACE "${_LBR}" "[" v "${v}")
    string(REPLACE "${_RBR}" "]" v "${v}")
    set(${out} "${v}" PARENT_SCOPE)
endfunction()

function(app_env_generate env_file out_header)
    if(NOT EXISTS "${env_file}")
        message(FATAL_ERROR
            "\nfirmware/.env not found.\n"
            "Copy firmware/.env.example to firmware/.env and fill in the values.\n")
    endif()
    set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS "${env_file}")

    file(READ "${env_file}" content)
    string(REPLACE ";" "${_SEMI}" content "${content}")
    string(REPLACE "[" "${_LBR}" content "${content}")
    string(REPLACE "]" "${_RBR}" content "${content}")
    string(REPLACE "\r" "" content "${content}")
    string(REPLACE "\n" ";" lines "${content}")

    foreach(line IN LISTS lines)
        string(STRIP "${line}" line)
        # Drop a UTF-8 BOM or other junk before the key (some editors add a BOM).
        string(REGEX REPLACE "^[^A-Za-z_#]+" "" line "${line}")
        if(line STREQUAL "" OR line MATCHES "^#")
            continue()
        endif()
        if(NOT line MATCHES "^([A-Za-z_][A-Za-z0-9_]*)[ \t]*=[ \t]*(.*)$")
            message(FATAL_ERROR "firmware/.env: cannot parse line: ${line}")
        endif()
        set(key "${CMAKE_MATCH_1}")
        set(val "${CMAKE_MATCH_2}")
        if(val MATCHES "^\"(.*)\"$" OR val MATCHES "^'(.*)'$")
            set(val "${CMAKE_MATCH_1}")
        endif()
        set(ENV_${key} "${val}")
        set(ENV_${key}_SET TRUE)
    endforeach()

    # --- required ------------------------------------------------------------------------
    foreach(key WIFI_SSID WIFI_PASSWORD SERVER_URL API_KEY)
        if(NOT ENV_${key}_SET)
            message(FATAL_ERROR "firmware/.env: required variable ${key} is missing (see .env.example)")
        endif()
    endforeach()
    foreach(key SERVER_URL API_KEY)
        if(ENV_${key} STREQUAL "")
            message(FATAL_ERROR "firmware/.env: ${key} must not be empty")
        endif()
    endforeach()
    if(ENV_WIFI_SSID STREQUAL "")
        message(WARNING "firmware/.env: WIFI_SSID is empty, WLAN will be disabled")
    endif()
    if(NOT ENV_SERVER_URL MATCHES "^http://")
        message(FATAL_ERROR "firmware/.env: SERVER_URL must start with http:// (got ${ENV_SERVER_URL})")
    endif()

    # --- optional with defaults ----------------------------------------------------------
    set(defaults
        "DEVICE_NAME=remote1"
        "CAMERA_FRAMESIZE=SVGA"
        "CAMERA_JPEG_QUALITY=12"
        "CAMERA_XCLK_MHZ=10"
        "CAMERA_HMIRROR=1"
        "CAMERA_VFLIP=0"
        "AF_TIMEOUT_MS=1500"
        "AF_MODE=continuous"
        "WIFI_TX_POWER_DBM=0"
        "BACKLIGHT_PCT=80"
        "DIM_AFTER_S=20"
        "DIM_PCT=10"
        "IDLE_AFTER_S=45"
        "IDLE_WIFI_POWER_SAVE=1")
    foreach(kv IN LISTS defaults)
        string(REGEX MATCH "^([^=]+)=(.*)$" _ "${kv}")
        if(NOT ENV_${CMAKE_MATCH_1}_SET OR ENV_${CMAKE_MATCH_1} STREQUAL "")
            set(ENV_${CMAKE_MATCH_1} "${CMAKE_MATCH_2}")
        endif()
    endforeach()

    set(framesizes QVGA VGA SVGA XGA HD SXGA UXGA FHD QXGA QSXGA)
    if(NOT ENV_CAMERA_FRAMESIZE IN_LIST framesizes)
        message(FATAL_ERROR "firmware/.env: CAMERA_FRAMESIZE must be one of: ${framesizes}")
    endif()
    foreach(key CAMERA_JPEG_QUALITY CAMERA_XCLK_MHZ CAMERA_HMIRROR CAMERA_VFLIP AF_TIMEOUT_MS BACKLIGHT_PCT
            DIM_AFTER_S DIM_PCT IDLE_AFTER_S IDLE_WIFI_POWER_SAVE)
        if(NOT ENV_${key} MATCHES "^[0-9]+$")
            message(FATAL_ERROR "firmware/.env: ${key} must be a number (got '${ENV_${key}}')")
        endif()
    endforeach()
    if(NOT ENV_AF_MODE MATCHES "^(single|continuous|off)$")
        message(FATAL_ERROR "firmware/.env: AF_MODE must be single, continuous or off")
    endif()
    if(NOT ENV_WIFI_TX_POWER_DBM MATCHES "^[0-9]+(\.[0-9]+)?$")
        message(FATAL_ERROR "firmware/.env: WIFI_TX_POWER_DBM must be a number like 8.5 (0 = default)")
    endif()
    if(NOT ENV_DEVICE_NAME MATCHES "^[A-Za-z0-9_-]+$")
        message(FATAL_ERROR "firmware/.env: DEVICE_NAME may only contain A-Z a-z 0-9 _ -")
    endif()

    foreach(key WIFI_SSID WIFI_PASSWORD SERVER_URL API_KEY DEVICE_NAME)
        _app_env_escape_c("${ENV_${key}}" C_${key})
    endforeach()
    string(REGEX REPLACE "/+$" "" C_SERVER_URL "${C_SERVER_URL}")

    set(h "// Generated from firmware/.env by cmake/app_env.cmake. Do not edit, do not commit.\n")
    string(APPEND h "#pragma once\n")
    string(APPEND h "#define APP_WIFI_SSID \"${C_WIFI_SSID}\"\n")
    string(APPEND h "#define APP_WIFI_PASSWORD \"${C_WIFI_PASSWORD}\"\n")
    string(APPEND h "#define APP_SERVER_URL \"${C_SERVER_URL}\"\n")
    string(APPEND h "#define APP_API_KEY \"${C_API_KEY}\"\n")
    string(APPEND h "#define APP_DEVICE_NAME \"${C_DEVICE_NAME}\"\n")
    string(APPEND h "#define APP_CAMERA_FRAMESIZE FRAMESIZE_${ENV_CAMERA_FRAMESIZE}\n")
    string(APPEND h "#define APP_CAMERA_FRAMESIZE_NAME \"${ENV_CAMERA_FRAMESIZE}\"\n")
    string(APPEND h "#define APP_CAMERA_JPEG_QUALITY ${ENV_CAMERA_JPEG_QUALITY}\n")
    string(APPEND h "#define APP_CAMERA_XCLK_MHZ ${ENV_CAMERA_XCLK_MHZ}\n")
    string(APPEND h "#define APP_CAMERA_HMIRROR ${ENV_CAMERA_HMIRROR}\n")
    string(APPEND h "#define APP_CAMERA_VFLIP ${ENV_CAMERA_VFLIP}\n")
    string(APPEND h "#define APP_AF_TIMEOUT_MS ${ENV_AF_TIMEOUT_MS}\n")
    string(APPEND h "#define APP_BACKLIGHT_PCT ${ENV_BACKLIGHT_PCT}\n")
    string(APPEND h "#define APP_DIM_AFTER_S ${ENV_DIM_AFTER_S}\n")
    string(APPEND h "#define APP_DIM_PCT ${ENV_DIM_PCT}\n")
    string(APPEND h "#define APP_IDLE_AFTER_S ${ENV_IDLE_AFTER_S}\n")
    string(APPEND h "#define APP_IDLE_WIFI_POWER_SAVE ${ENV_IDLE_WIFI_POWER_SAVE}\n")
    string(TOUPPER "${ENV_AF_MODE}" AF_MODE_UP)
    string(APPEND h "#define APP_AF_MODE_${AF_MODE_UP} 1\n")
    string(APPEND h "#define APP_AF_MODE_NAME \"${ENV_AF_MODE}\"\n")
    string(APPEND h "#define APP_WIFI_TX_POWER_DBM ((float)(${ENV_WIFI_TX_POWER_DBM}))\n")

    # Only touch the header if the content changed (avoids needless rebuilds).
    file(WRITE "${out_header}.tmp" "${h}")
    configure_file("${out_header}.tmp" "${out_header}" COPYONLY)
endfunction()

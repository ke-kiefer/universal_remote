idf_component_register(
    SRCS
        "main.c"
        "board/board.c"
        "board/battery.c"
        "board/camera.c"
        "net/wifi.c"
        "net/api_client.c"
        "ui/ui.c"
    INCLUDE_DIRS "." "board" "net" "ui"
    EMBED_FILES "fonts/Montserrat-SemiBold-latin1.ttf"
    REQUIRES
        esp_driver_gpio
        esp_driver_ledc
        esp_driver_spi
        esp_driver_i2c
        esp_adc
        esp_lcd
        esp_timer
        esp_psram
        esp_wifi
        esp_netif
        esp_event
        esp_http_client
        nvs_flash
)

# Generate app_env.h from firmware/.env (skipped during the requirements scan).
if(NOT CMAKE_BUILD_EARLY_EXPANSION)
    include(${CMAKE_CURRENT_LIST_DIR}/../cmake/app_env.cmake)
    set(APP_ENV_DIR "${CMAKE_BINARY_DIR}/app_env")
    file(MAKE_DIRECTORY "${APP_ENV_DIR}")
    app_env_generate("${CMAKE_CURRENT_LIST_DIR}/../.env" "${APP_ENV_DIR}/app_env.h")
    target_include_directories(${COMPONENT_LIB} PRIVATE "${APP_ENV_DIR}")
endif()

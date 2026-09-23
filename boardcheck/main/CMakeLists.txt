idf_component_register(SRCS "boardcheck_main.c"
                       INCLUDE_DIRS "."
                       REQUIRES iis3dwb esp_driver_gpio esp_timer spi_flash)

target_link_libraries(${COMPONENT_LIB} INTERFACE m)

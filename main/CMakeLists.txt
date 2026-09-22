idf_component_register(SRCS "main.c"
                       INCLUDE_DIRS "."
                       REQUIRES iis3dwb wifi_manager config_manager serial_protocol sensor_streamer esp_driver_gpio esp_driver_ledc)

# Link math library for sqrtf()/cosf()
target_link_libraries(${COMPONENT_LIB} INTERFACE m)

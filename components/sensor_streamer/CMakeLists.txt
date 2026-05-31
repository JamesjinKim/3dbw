idf_component_register(
    SRCS "sensor_streamer.c"
    INCLUDE_DIRS "."
    REQUIRES iis3dwb esp_timer lwip
)

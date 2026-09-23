idf_component_register(
    SRCS "sensor_streamer.c"
    INCLUDE_DIRS "."
    REQUIRES iis3dwb esp_timer lwip esp_driver_gpio esp_driver_usb_serial_jtag esp_driver_uart
)

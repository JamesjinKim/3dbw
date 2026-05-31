idf_component_register(
    SRCS "serial_protocol.c"
    INCLUDE_DIRS "."
    REQUIRES esp_driver_usb_serial_jtag vfs json app_update esp_wifi
             config_manager wifi_manager
)

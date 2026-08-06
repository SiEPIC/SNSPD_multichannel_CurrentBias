idf_component_register(
    SRCS
        "app_main.cpp"
    REQUIRES
        esp_driver_gpio
        esp_driver_spi
        esp_timer
        app
)

target_compile_features(${COMPONENT_LIB} PRIVATE cxx_std_17)

idf_component_register(
  SRCS  "task_comms.cpp" "coordinator.cpp" "channel.cpp"
  INCLUDE_DIRS "include"
  REQUIRES nvs_flash
           adc
           dac
           platform
           no_os
)

target_compile_options(${COMPONENT_LIB} PRIVATE -Wno-missing-field-initializers)

idf_component_register(
  SRCS  "task_comms.cpp" "coordinator.cpp" "channel.cpp"
  INCLUDE_DIRS "include"
  REQUIRES nvs_flash
           adc
           dac
           platform
           no_os
)

#Suppresses warning from not initializing ADC gpio configs, but it is not used on the PCB.
target_compile_options(${COMPONENT_LIB} PRIVATE -Wno-missing-field-initializers)

# Suppresses warning for not having break in run_sweep() for phase change.
# Breaks are removed for a reason.
target_compile_options(${COMPONENT_LIB} PRIVATE -Wimplicit-fallthrough=0) 

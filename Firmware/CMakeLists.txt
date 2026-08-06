cmake_minimum_required(VERSION 3.16)

# Adds external drivers as dependency
set(EXTRA_COMPONENT_DIRS "components/drivers")
set(SDKCONFIG_DEFAULTS "sdkconfig.defaults")

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(multi_channel_voltage_source_fw)

cmake_minimum_required(VERSION 3.20)
project(sample)

add_library(core STATIC core.cc)
add_executable(app main.cc)
target_link_libraries(app PRIVATE core)

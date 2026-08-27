cmake_minimum_required(VERSION 3.17)
project(trayyd LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_EXTENSIONS OFF)

add_compile_options(-Wall -Wpedantic)

add_executable(trayyd
    main.cpp
    kwin_dbus.cpp
    trayd_service.cpp
    button_detect.cpp
    uinput_forward.cpp
    topology.cpp
)

find_package(PkgConfig REQUIRED)
pkg_check_modules(DBUS REQUIRED dbus-1)

target_include_directories(trayyd PRIVATE ${DBUS_INCLUDE_DIRS})
target_link_libraries(trayyd PRIVATE ${DBUS_LDFLAGS})

install(TARGETS trayyd DESTINATION bin)

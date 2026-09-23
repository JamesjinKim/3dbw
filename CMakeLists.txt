# IIS3DWB Vibration Sensor Project for ESP32-S3
# CMakeLists in this exact order for cmake to work correctly
cmake_minimum_required(VERSION 3.16)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)

# 프로젝트명이 산출물 이름을 결정한다 → build/iis3dwb.bin / .elf / .map
# (과거 이름은 tsl2591_sensor 였다. 배포 문서가 build/<이름>.bin 을 참조하므로
#  이 값을 바꾸면 그 경로들도 함께 갱신해야 한다)
project(iis3dwb)

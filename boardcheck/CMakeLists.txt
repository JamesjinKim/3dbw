# IIS3DWB 보드 검사 펌웨어 (독립 ESP-IDF 프로젝트)
#
# 상위 프로젝트(../)와 완전히 분리된 별도 프로젝트다. 공유하는 것은
# components/iis3dwb 하나뿐이고, wifi_manager·config_manager·serial_protocol·
# sensor_streamer 는 쓰지 않는다.
#
# 이렇게 분리한 이유:
#   · 스트리밍/설정 코드가 바뀌어도 검사 결과가 흔들리지 않는다
#   · NVS 설정이 없어도 꽂자마자 검사가 돈다 (WiFi 비밀번호 불필요)
#   · 부팅 후 20초면 판정이 끝난다
cmake_minimum_required(VERSION 3.16)

# 센서 드라이버는 상위 프로젝트와 동일한 소스를 그대로 쓴다.
# (사본을 만들면 드라이버 수정이 검사에 반영되지 않아 의미가 없어진다)
set(EXTRA_COMPONENT_DIRS "${CMAKE_CURRENT_LIST_DIR}/../components")

# EXTRA_COMPONENT_DIRS 는 그 폴더의 **모든** 컴포넌트를 빌드 대상에 올린다.
# 그대로 두면 wifi_manager·config_manager·serial_protocol·sensor_streamer·tsl2591
# 까지 컴파일되어, 검사와 무관한 코드의 오류·경고가 검사 빌드를 막는다.
# main 만 지정하면 main 의 REQUIRES(iis3dwb 등)와 공통 컴포넌트만 빌드된다.
set(COMPONENTS main)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)

project(iis3dwb_boardcheck)

#
# oskar/ms/test/CMakeLists.txt
#

set(name ms_test)

set(${name}_SRC
    Test_MeasurementSet.cpp
)
add_executable(${name} ${${name}_SRC})
include_directories(${PROJECT_SOURCE_DIR}/extern/gtest-1.7.0/include)
set(GTEST_INCLUDE_DIR ${PROJECT_SOURCE_DIR}/extern/gtest-1.7.0/include/gtest)
set(GTEST_LIBRARY_DIR ${PROJECT_SOURCE_DIR}/extern/gtest-1.7.0/lib/gtest)
target_link_libraries(${name} oskar_ms)
#add_test(ms_test ${name})

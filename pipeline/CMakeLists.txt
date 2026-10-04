# The development executable has a separate identity. No runtime switch enables
# simulator profiles in the production executable.
if(UNIX)
  find_package(Python3 3.9 REQUIRED COMPONENTS Interpreter)
  add_library(pipeline_core STATIC Profile.cpp Backend.cpp Process.cpp HttpProbe.cpp Agent.cpp Security.cpp Protocol.cpp Channel.cpp Group.cpp)
  target_include_directories(pipeline_core PUBLIC ${PROJECT_SOURCE_DIR})
  target_link_libraries(pipeline_core PUBLIC tdactor tdutils nlohmann_json::nlohmann_json Boost::headers)
  target_link_libraries(pipeline_core PUBLIC cocoon tdnet_ported ${OPENSSL_SSL_LIBRARY} ${OPENSSL_CRYPTO_LIBRARY})
  target_include_directories(pipeline_core SYSTEM PUBLIC ${OPENSSL_INCLUDE_DIR})
  target_compile_definitions(pipeline_core PRIVATE
    PIPELINE_PYTHON="${Python3_EXECUTABLE}"
    PIPELINE_SIMULATOR="${CMAKE_CURRENT_SOURCE_DIR}/simulator.py")

  add_executable(pipeline-agent main.cpp)
  target_link_libraries(pipeline-agent PRIVATE pipeline_core)
  add_executable(pipeline-agent-dev EXCLUDE_FROM_ALL main.cpp)
  target_compile_definitions(pipeline-agent-dev PRIVATE COCOON_PIPELINE_DEV=1)
  target_link_libraries(pipeline-agent-dev PRIVATE pipeline_core)
  add_executable(test-pipeline-profile EXCLUDE_FROM_ALL ../test/test-pipeline-profile.cpp)
  target_link_libraries(test-pipeline-profile PRIVATE pipeline_core)
  add_executable(pipeline-dev-cert EXCLUDE_FROM_ALL dev-cert.cpp)
  target_link_libraries(pipeline-dev-cert PRIVATE pipeline_core)
  add_executable(test-pipeline-membership EXCLUDE_FROM_ALL ../test/test-pipeline-membership.cpp)
  target_link_libraries(test-pipeline-membership PRIVATE pipeline_core)
endif()

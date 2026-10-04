# The development executable has a separate identity. No runtime switch enables
# simulator profiles in the production executable.
if(UNIX)
  find_package(Python3 3.9 REQUIRED COMPONENTS Interpreter)
  set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/sglang-models.json")
  file(READ "${CMAKE_CURRENT_SOURCE_DIR}/sglang-models.json" SGLANG_MODELS)
  configure_file(sglang-models.h.in sglang-models.h @ONLY)
  add_library(pipeline_core STATIC Profile.cpp Backend.cpp Sglang.cpp Vllm.cpp Process.cpp HttpProbe.cpp Agent.cpp Security.cpp Protocol.cpp Channel.cpp Group.cpp Network.cpp Gate.cpp)
  target_include_directories(pipeline_core PRIVATE ${CMAKE_CURRENT_BINARY_DIR})
  target_include_directories(pipeline_core PUBLIC ${PROJECT_SOURCE_DIR})
  target_link_libraries(pipeline_core PUBLIC tdactor tdutils nlohmann_json::nlohmann_json Boost::headers)
  target_link_libraries(pipeline_core PUBLIC cocoon tdnet_ported ${OPENSSL_SSL_LIBRARY} ${OPENSSL_CRYPTO_LIBRARY})
  target_include_directories(pipeline_core SYSTEM PUBLIC ${OPENSSL_INCLUDE_DIR})
  target_compile_definitions(pipeline_core PRIVATE
    PIPELINE_PYTHON="${Python3_EXECUTABLE}"
    PIPELINE_SIMULATOR="${CMAKE_CURRENT_SOURCE_DIR}/simulator.py"
    PIPELINE_HELPER_DIR="${CMAKE_CURRENT_SOURCE_DIR}"
    PIPELINE_VLLM_HELPER="${CMAKE_CURRENT_SOURCE_DIR}/vllm-helper.py"
    PIPELINE_SGLANG_HELPER="${CMAKE_CURRENT_SOURCE_DIR}/sglang-helper.py"
    PIPELINE_NETWORK_HELPER="${CMAKE_CURRENT_SOURCE_DIR}/network-helper.py")
  if(CMAKE_SYSTEM_NAME STREQUAL "Linux")
    add_executable(pipeline-backend-sandbox backend-sandbox.cpp)
    target_compile_definitions(pipeline_core PRIVATE PIPELINE_SANDBOX="$<TARGET_FILE:pipeline-backend-sandbox>")
    add_dependencies(pipeline_core pipeline-backend-sandbox)
  endif()

  add_executable(pipeline-agent main.cpp)
  target_link_libraries(pipeline-agent PRIVATE pipeline_core)
  add_executable(pipeline-agent-dev EXCLUDE_FROM_ALL main.cpp)
  target_compile_definitions(pipeline-agent-dev PRIVATE COCOON_PIPELINE_DEV=1)
  target_link_libraries(pipeline-agent-dev PRIVATE pipeline_core)
  add_executable(test-pipeline-profile EXCLUDE_FROM_ALL ../test/test-pipeline-profile.cpp)
  target_link_libraries(test-pipeline-profile PRIVATE pipeline_core)
  add_executable(test-pipeline-sglang EXCLUDE_FROM_ALL ../test/test-pipeline-sglang.cpp)
  target_link_libraries(test-pipeline-sglang PRIVATE pipeline_core)
  add_executable(test-pipeline-vllm EXCLUDE_FROM_ALL ../test/test-pipeline-vllm.cpp)
  target_link_libraries(test-pipeline-vllm PRIVATE pipeline_core)
  add_executable(pipeline-dev-cert EXCLUDE_FROM_ALL dev-cert.cpp)
  target_link_libraries(pipeline-dev-cert PRIVATE pipeline_core)
  add_executable(test-pipeline-membership EXCLUDE_FROM_ALL ../test/test-pipeline-membership.cpp)
  target_link_libraries(test-pipeline-membership PRIVATE pipeline_core)
endif()

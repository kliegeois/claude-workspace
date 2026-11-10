# Initial flags for new Linux GCC/Clang builds in this workspace.
# Keep full debug information; compress it in objects and linked ELF files.
# Existing CMake cache entries and explicit -D flag settings take precedence.
set(CMAKE_C_FLAGS "$ENV{CFLAGS} -gz=zstd" CACHE STRING "Host C compiler flags")
set(CMAKE_CXX_FLAGS "$ENV{CXXFLAGS} -gz=zstd" CACHE STRING "Host C++ compiler flags")

foreach(kind EXE SHARED MODULE)
  set(CMAKE_${kind}_LINKER_FLAGS
    "$ENV{LDFLAGS} -Wl,--compress-debug-sections=zstd"
    CACHE STRING "Host ${kind} linker flags")
endforeach()

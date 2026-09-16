# backend/synth/native/Makefile
CXX ?= c++
CXXFLAGS := -std=c++20 -O3 -ffast-math -fno-math-errno -Wall -Wextra -fPIC
UNAME_S := $(shell uname -s)
ifeq ($(UNAME_S),Darwin)
  LIBEXT := dylib
  SHARED := -dynamiclib
else ifneq (,$(findstring MINGW,$(UNAME_S))$(findstring MSYS,$(UNAME_S)))
  # MinGW/MSYS2 (Windows): backend/synth/_lib.py loads libgnsssynth.dll via
  # ctypes.CDLL -- MinGW GCC auto-exports extern "C" symbols from a -shared
  # build with no separate .def/dllexport step needed. MSVCRT's <cmath>
  # only defines M_PI etc. under this macro (glibc/macOS libc define them
  # unconditionally) -- harmless to define, but scoped to this branch so
  # the POSIX build's flags stay exactly as they were.
  # Static-link the MinGW runtime so the DLL doesn't need libstdc++-6.dll /
  # libgcc_s_seh-1.dll / libwinpthread-1.dll on the loading process's PATH
  # (ctypes.CDLL has no notion of an rpath to point at this directory).
  CXXFLAGS += -D_USE_MATH_DEFINES
  LIBEXT := dll
  SHARED := -shared -static-libgcc -static-libstdc++ -static
else
  LIBEXT := so
  SHARED := -shared
endif
LIB := libgnsssynth.$(LIBEXT)
SRC := $(wildcard *.cpp)
OBJ := $(SRC:.cpp=.o)

all: $(LIB)
$(LIB): $(OBJ)
	$(CXX) $(CXXFLAGS) $(SHARED) -o $@ $(OBJ)
%.o: %.cpp
	$(CXX) $(CXXFLAGS) -c -o $@ $<
clean:
	rm -f $(OBJ) $(LIB)
.PHONY: all clean

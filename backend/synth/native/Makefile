# backend/synth/native/Makefile
CXX ?= c++
CXXFLAGS := -std=c++20 -O3 -ffast-math -fno-math-errno -Wall -Wextra -fPIC
UNAME_S := $(shell uname -s)
ifeq ($(UNAME_S),Darwin)
  LIBEXT := dylib
  SHARED := -dynamiclib
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

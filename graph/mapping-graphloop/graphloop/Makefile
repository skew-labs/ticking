# Economic Machine graph-loop native core.  Plain g++ lines like the other EM native builds
# (g++ -std=c++20 -O2 ... -lcryptopp -lzstd -pthread); Arrow/Parquet only for build/export tools.
CXX      ?= g++
ARCH     ?= -march=x86-64-v3 -mtune=znver4
CXXFLAGS ?= -std=c++20 -O2 $(ARCH) -g1 -Wall -Wextra -Wno-unused-parameter -Wno-missing-field-initializers -pthread
INC       = -Iinclude
CORE      = -lcryptopp -lzstd -pthread
ARROW     = -larrow -lparquet
B         = build
HDR       = $(wildcard include/machine/graphloop/*.hpp)

all: $(B)/glbuild $(B)/glserve $(B)/glexport $(B)/glbench $(B)/test_graphloop

$(B):
	mkdir -p $(B)
$(B)/glbuild: src/glbuild.cpp $(HDR) | $(B)
	$(CXX) $(CXXFLAGS) $(INC) $< -o $@ $(ARROW) $(CORE)
$(B)/glserve: src/glserve.cpp $(HDR) | $(B)
	$(CXX) $(CXXFLAGS) $(INC) $< -o $@ $(CORE)
$(B)/glexport: src/glexport.cpp $(HDR) | $(B)
	$(CXX) $(CXXFLAGS) $(INC) $< -o $@ $(ARROW) $(CORE)
$(B)/glbench: src/glbench.cpp $(HDR) | $(B)
	$(CXX) $(CXXFLAGS) $(INC) $< -o $@ $(CORE)
$(B)/glsynth: src/glsynth.cpp $(HDR) | $(B)
	$(CXX) $(CXXFLAGS) $(INC) $< -o $@ $(ARROW) $(CORE)
$(B)/test_graphloop: tests/test_graphloop.cpp $(HDR) | $(B)
	$(CXX) -std=c++20 -O1 $(ARCH) -g -Wall -Wextra -Wno-unused-parameter -Wno-missing-field-initializers -pthread $(INC) $< -o $@ $(CORE)
$(B)/test_graphloop-sanitize: tests/test_graphloop.cpp $(HDR) | $(B)
	$(CXX) -std=c++20 -O1 -g -fsanitize=address,undefined -fno-omit-frame-pointer -pthread $(INC) $< -o $@ $(CORE)

test: $(B)/test_graphloop $(B)/test_graphloop-sanitize
	./$(B)/test_graphloop && ./$(B)/test_graphloop-sanitize

clean:
	rm -rf $(B)
.PHONY: all test clean

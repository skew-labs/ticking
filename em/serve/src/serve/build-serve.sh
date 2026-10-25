#!/bin/bash
# src/serve/build-serve.sh: build emv2-serve and its tests without CMake (works on skew and the VM after tools/sync.sh). Links the reader libraries that exist (src/lib/graph*.cpp, ledger*.cpp)
# with HAVE_EMV2_GRAPH / HAVE_EMV2_LEDGER; missing ones stay null through the weak references in store_v2.cpp.
# ASAN=1: -O1 ASan+UBSan test build.
set -e
D=${D:-$(cd "$(dirname "$0")/../../.." && pwd)}; O=${O:-$D/out}; mkdir -p $O   # D = the tools/sync.sh destination (has emv2/, graphloop/)
INC="-I$D/emv2/include -I$D/graphloop/include -I$D/emv2/src/lib"
LIBS=""; DEFS=""
G=$(ls $D/emv2/src/lib/graph*.cpp 2>/dev/null || true); L=$(ls $D/emv2/src/lib/ledger*.cpp 2>/dev/null || true)
[ -n "$G" ] && { LIBS="$LIBS $G"; DEFS="$DEFS -DHAVE_EMV2_GRAPH=1"; }
[ -n "$L" ] && { LIBS="$LIBS $L"; DEFS="$DEFS -DHAVE_EMV2_LEDGER=1"; }
LINK="-lcryptopp -lpthread $(ls /usr/lib/x86_64-linux-gnu/libzstd.so >/dev/null 2>&1 && echo -lzstd)"
F="-std=c++20 -O2 -g -Wall -Wextra -Wno-unused-parameter -Wno-misleading-indentation -march=x86-64-v3 $INC $DEFS"
SRC="$D/emv2/src/serve/routes.cpp $D/emv2/src/serve/registry.cpp $D/emv2/src/serve/store_synth.cpp $D/emv2/src/serve/store_v2.cpp $LIBS"
echo "readers:${G:+ graph}${L:+ ledger}"
if [ "${ASAN:-0}" = 1 ]; then
  S="-std=c++20 -O1 -g -fsanitize=address,undefined -fno-omit-frame-pointer $INC $DEFS"
  nice -n 19 g++ $S -UNDEBUG $SRC $D/emv2/tests/serve_basic.cpp -o $O/serve_basic.asan $LINK
  ls -la $O; exit 0
fi
nice -n 19 g++ $F $SRC $D/emv2/src/serve/main.cpp -o $O/emv2-serve $LINK &
nice -n 19 g++ $F -UNDEBUG $SRC $D/emv2/tests/serve_basic.cpp -o $O/serve_basic $LINK &
nice -n 19 g++ $F -UNDEBUG $D/emv2/tests/serve_sandbox.cpp -o $O/serve_sandbox &
nice -n 19 g++ $F -UNDEBUG $SRC $D/emv2/tests/serve_v2store.cpp -o $O/serve_v2store $LINK &
wait
ls -la $O

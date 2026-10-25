#!/bin/bash
# tools/integrate.sh [HOST:DIR]  (core integration gate; default skew:/srv/skew/emv2-dev/integ)
# Syncs the tree, then on the host: Release build + ctest (incl. economic-machine suite), and an
# ASan/UBSan build + ctest. Builds run under nice/ionice with --parallel 4 (skew rules).
# Prints one JSON line per stage; exit 1 if any stage failed.
set -uo pipefail
dest=${1:-skew:/srv/skew/emv2-dev/integ}
host=${dest%%:*}; dir=${dest#*:}
here=$(cd "$(dirname "$0")" && pwd)
"$here/sync.sh" "$dest" >/dev/null || { echo '{"stage":"sync","ok":false}'; exit 1; }
extra=${EMV2_CMAKE_EXTRA:-}
ssh "$host" "bash -s" <<REMOTE
set -uo pipefail
cd '$dir'
run() { local name=\$1; shift; local t0=\$(date +%s); if "\$@" > "\$name.log" 2>&1; then ok=true; else ok=false; fi
        echo "{\"stage\":\"\$name\",\"ok\":\$ok,\"s\":\$((\$(date +%s)-t0)),\"log\":\"$dir/\$name.log\"}"; \$ok; }
N="nice -n 19 ionice -c3"
rc=0
run cfg_release \$N cmake -S emv2 -B build -DEMV2_BUILDERS=OFF $extra || rc=1
run build_release \$N cmake --build build --parallel 4 -- -k || rc=1
run ctest_release \$N ctest --test-dir build --output-on-failure --timeout 900 || rc=1
run cfg_san \$N cmake -S emv2 -B build-san -DEMV2_BUILDERS=OFF -DEMV2_SANITIZE=ON -DEMV2_EM_TESTS=OFF $extra || rc=1
run build_san \$N cmake --build build-san --parallel 4 -- -k || rc=1
run ctest_san env ASAN_OPTIONS=detect_leaks=1:abort_on_error=1 UBSAN_OPTIONS=print_stacktrace=1:halt_on_error=1 \$N ctest --test-dir build-san --output-on-failure --timeout 1800 || rc=1
grep -h -E "tests passed|tests failed" ctest_release.log ctest_san.log 2>/dev/null
exit \$rc
REMOTE

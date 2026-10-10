#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
test "$(uname -s)" = Linux
mkdir -p .build/generated .build/bin
python3 scripts/fetch-proto.py
protoc -I engine/vendor/yellowstone --cpp_out=.build/generated --grpc_out=.build/generated --plugin=protoc-gen-grpc="$(command -v grpc_cpp_plugin)" engine/vendor/yellowstone/*.proto
python3 -m grpc_tools.protoc -I engine/vendor/yellowstone --python_out=engine/vendor/yellowstone --grpc_python_out=engine/vendor/yellowstone engine/vendor/yellowstone/*.proto
flags=(-std=c++20 -O2 -pthread -Wall -Wextra -Wno-deprecated-declarations -Wno-misleading-indentation -Iengine/engine -I.build/generated)
g++ "${flags[@]}" -fPIC -shared engine/engine/core.cpp -o engine/engine/libslotline.so
g++ "${flags[@]}" -fPIC -shared engine/engine/observer.cpp -lcrypto -lz -o engine/engine/libslotline_observer.so
cp engine/engine/libslotline_observer.so engine/engine/libslotline_execution.so
g++ "${flags[@]}" engine/engine/core.cpp engine/engine/core_checks.cpp -o .build/bin/core-checks
g++ "${flags[@]}" engine/engine/observer.cpp engine/engine/observer_checks.cpp -lcrypto -lz -o .build/bin/observer-checks
g++ "${flags[@]}" engine/engine/observer.cpp engine/engine/observer_node.cpp -lcrypto -lz -o engine/engine/slotline-observer
g++ "${flags[@]}" engine/engine/hyperliquid_public.cpp $(pkg-config --cflags --libs libcurl sqlite3 libzstd) -o .build/bin/venue-collector
objects=()
for source in engine/engine/native_archive.cpp engine/engine/archive_capsule.cpp .build/generated/*.cc; do
  object=".build/$(basename "$source").o"
  g++ "${flags[@]}" $(pkg-config --cflags grpc++ protobuf openssl zlib libdeflate libzstd) -c "$source" -o "$object"
  objects+=("$object")
done
for pair in 'metadata_recorder.cpp metadata-recorder' 'native_archive_checks.cpp archive-checks' 'archive_capsule_checks.cpp capsule-checks' 'archive_compactor.cpp archive-compactor' 'metadata_transport_checks.cpp transport-checks' 'native_archive_fault_checks.cpp fault-checks'; do
  read -r file binary <<< "$pair"
  extra=();if test "$binary" = fault-checks;then extra=(-Wl,--wrap=write -Wl,--wrap=fdatasync -Wl,--wrap=rename);fi
  g++ "${flags[@]}" engine/engine/"$file" "${objects[@]}" "${extra[@]}" $(pkg-config --cflags --libs grpc++ protobuf openssl zlib libdeflate libzstd) -o .build/bin/"$binary"
done
printf 'Native collectors and verification binaries built. No live connection started.\n'

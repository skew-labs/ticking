#!/bin/bash
# tools/sync.sh HOST:DIR  (or a local DIR): copy emv2 plus the two read-only dependencies with the
# layout CMakeLists.txt expects:  DIR/{emv2, vendor/economic-machine/<native subset>, graphloop/include}
# Example: tools/sync.sh skew:/srv/skew/emv2-dev/graph   then on the host:
#   (VM: EMV2_SSH="ssh -i /Users/huhyul/Downloads/arbitrumkey.pem -o IdentitiesOnly=yes" tools/sync.sh azureuser@172.171.247.70:/data/emv2-dev/<m>)
#   cmake -S DIR/emv2 -B DIR/build -DEMV2_BUILDERS=OFF && cmake --build DIR/build --parallel 4 && ctest --test-dir DIR/build
set -euo pipefail
dest=${1:?usage: tools/sync.sh HOST:DIR | DIR}
root=$(cd "$(dirname "$0")/../.." && pwd)        # /Users/huhyul/em-mapping
ssh_cmd=${EMV2_SSH:-ssh}   # e.g. EMV2_SSH="ssh -i KEY -o IdentitiesOnly=yes" for the VM
ex=(-e "$ssh_cmd" --exclude /build/ --exclude /build-san/ --exclude '.spill-*' --exclude '__pycache__/' --exclude '*.o')
if [[ $dest == *:* ]]; then host=${dest%%:*}; dir=${dest#*:}; $ssh_cmd "$host" "mkdir -p '$dir/vendor/economic-machine/products/pacifica_muse' '$dir/vendor/economic-machine/markets/solana' '$dir/graphloop'"; else dir=$dest; mkdir -p "$dir/vendor/economic-machine/products/pacifica_muse" "$dir/vendor/economic-machine/markets/solana" "$dir/graphloop"; fi
rsync -a --delete "${ex[@]}" "$root/emv2/" "$dest/emv2/"
rsync -a --delete -e "$ssh_cmd" "$root/vendor/economic-machine/native/" "$dest/vendor/economic-machine/native/"
rsync -a --delete -e "$ssh_cmd" "$root/vendor/economic-machine/products/pacifica_muse/native/" "$dest/vendor/economic-machine/products/pacifica_muse/native/"
rsync -a --delete -e "$ssh_cmd" "$root/vendor/economic-machine/markets/solana/v6/" "$dest/vendor/economic-machine/markets/solana/v6/"
rsync -a --delete -e "$ssh_cmd" "$root/graphloop/include/" "$dest/graphloop/include/"
echo "synced to $dest"

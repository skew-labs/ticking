# 큰 체인 재개 안내 (polygon, base, ethereum 전체) — 2026-10-20 23:00 KST 기준

**결정 (컨트롤러, 23:00 KST):** 02:00 KST에 VM을 일찍 끄는 안으로 가요.
- 큰 체인 ledger(polygon, base, ethereum 전체)는 NVMe를 단 뒤 VM을 다시 켜서 지어요. core의 WETH9 보정을 반영한 바이너리로 지어요.
- 오늘 VM에서 마치는 것: robinhood, optimism, arbitrum 전체 빌드, unichain 결정성 B·C, ethereum graph-only(`c1ae4fffe3f6ef5d`, 옛 id 규칙) 업로드

## 멈춘 시점의 상태
| 체인 | 상태 | 남은 것 |
|---|---|---|
| polygon | map 받기를 13:59 UTC에 멈췄어요(addresses 52G는 다 받았고 edges는 34G/144G까지). 디스크를 비우려고 VM 사본은 지웠어요. Blob 원본(`polygon/map/{addresses,edges}`)은 그대로예요. | graph v2 빌드, ledger 스트리밍(pass마다 업로드 hook, core 담당) |
| base | map 입력 사본은 12:32에 디스크 때문에 지웠어요. ledger counts는 끝났어요(13,575,760,513, map과 일치, verify 9/9). | graph v2 빌드, ledger 스트리밍 |
| ethereum | graph-only `c1ae4fffe3f6ef5d`(옛 id 규칙, WETH9 보정 전)를 Blob에 올려요. ledger counts는 verify 9/9였어요. | WETH9 보정 뒤 새 id로 graph와 ledger를 전부 다시 지어요(입력 224G + 출력 약 290G) |
| polygon 정확값 | ledger focus 결과 `bal_focus.tsv`(3.08GB, 12:54 UTC)가 VM `/data/emv2-dev/ledger/out/polygon/`에 있어요. VM을 끄기 전에 Blob `emv2/verify/20261020/polygon/`에 올려요. | verify 전수 대조 |

## 2026-10-21 00:20 KST 추가: optimism·arbitrum ledger도 재개 묶음으로
- **optimism `faf50b8025dbe29b`, arbitrum `5bb7feeb57232401`:** graph-only로 publish했어요. ledger는 VM 마감 안에 끝낼 수 없었어요.
  - arbitrum: per-pass 2에서 RSS 52G로 VM 전체 OOM이 났어요. pass마다 57G를 다시 읽어서 약 8분씩 걸려요.
  - optimism: per-pass 1인데도 뜨거운 파티션(1.05억 이벤트)이 40G를 썼어요. pass마다 약 4분이에요.
- **재개할 때:** 아래 세 가지를 고친 ledger 바이너리로 새 id 규칙의 전체 빌드를 지어요(core 정리 15:20 UTC). graph는 그대로 다시 써도 돼요(layout 같음).
  1. **핫 토큰 분할:** 이벤트가 평균보다 몇 배 많은 토큰은 따로 떼어 내요. 파티션 안에서도 블록 구간으로 나눠요.
  2. **입력은 한 번만 읽기:** 첫 패스에서 파티션별 spill 파일을 쓰고, 이후 패스는 자기 spill만 읽어요. 지금은 패스마다 입력 전체(arbitrum 57G, optimism 32G)를 다시 읽어요.
  3. **메모리 공식:** 평균 파티션이 아니라 가장 큰 파티션을 기준으로 잡아요.
  - VM에는 큰 ledger 하나만 돌려요.

## 재개 순서 (NVMe 장착 + WETH9 바이너리 준비 뒤)
1. **VM을 켜요.** 같은 디스크라면 `/data/emv2-dev/{ops,opsbuild,graph,ledger}`가 남아 있어요. 새 VM이면 `tools/sync.sh`로 소스를 보내고, cmake로 `EMV2_BUILDERS=ON`, `g++-14`를 써서 빌드해요.
   - 필요한 패키지: g++-14, liburing-dev, Arrow·Parquet 26 dev. apt는 `NEEDRESTART_MODE=l`로 돌려요.
2. **새 build_id를 만들어요.** 최신 emv2-manifest를 써요. 형식이 id에 들어간 규칙이에요.
   ```
   sudo python3 /data/emv2-dev/ops/emv2_inputs.py ids/<c>.inputs.json <map addresses/> <map edges/> [<tt/> <blocks/>]
   /data/emv2-dev/opsbuild/build/emv2-manifest build-id <c> ids/<c>.inputs.json
   ```
3. **디스크 규칙:** /data는 880G예요. 한 번에 큰 체인 하나씩 돌려요.
   - 순서: polygon → publish·정리 → base → publish·정리 → ethereum 전체
   - `emv2-diskguard`(여유 45G 아래면 EMV2 레인을 멈춤)를 먼저 띄워요.
4. **graph:**
   ```
   sudo systemd-run --unit emv2-stage-<c> ... bash ops/stage_map_inputs.sh <c>
   sudo systemd-run --unit emv2-graph-<c> -p MemoryMax=40G -p OOMScoreAdjust=800 -E GRAPH_BIN=/data/emv2-dev/graph/cm2/build bash ops/run_graph.sh <c> <id> /data/emv2-in/<c>/map 16
   ```
   - 피크: polygon 입력 196G + 임시·출력 약 200G, base 142G + 약 150G
5. **ledger:**
   - polygon·base는 스트리밍이에요. pass마다 sha256 → `objects/<sha>` 업로드 → 로컬 삭제 순서이고, 마지막에 `merge --no-verify`를 해요. hook은 core가 넣어요.
   - ethereum 전체는 `run_chain.sh`로 돌리고, P와 PER_PASS는 메모리 공식(이벤트/P×per-pass×140B)으로 정해요.
6. **올리기:** `finish_build.sh <dir>`(merge → verify → publish)를 써요. 이제 publish는 같은 id의 manifest가 다르면 거부해요.
7. **skew:** NVMe를 `/srv/emv2`에 마운트한 뒤 `emv2_fetch.py <c> <id> --exclude-kinds g.edge.exact,g.hub.cold`로 받아요(`ops/NVME.md` 참고).

## 비용 메모
- 재개에 VM이 6~8시간쯤 필요해요(E16ads_v6 시간당 약 $1.05).
- 같은 지역 Blob 읽기는 무료예요. skew로 받는 hot 이미지는 $0.087/GB예요.

## ledger 파티션 편중 (robinhood 실측, 2026-10-20)
- **무슨 일:** robinhood(P=32)의 파티션 2에 이벤트가 3.29억 건 몰렸어요. 평균 약 0.56억의 6배이고 다른 파티션은 0.2~0.4억이에요.
  - 그래서 메모리 공식(이벤트/P × per-pass × 140B)으로는 17~32G였는데, 실제로는 50G를 넘었어요. 30·34·46G 상한에서 throttle과 OOM이 났어요.
  - 그 패스 하나만 989초가 걸렸어요(다른 패스는 약 1~4분).
- **고칠 방향(core):** 뜨거운 토큰을 따로 떼어 내 파티션을 나눠요. 한 토큰이 이벤트의 0.5%를 넘으면 그 토큰만 여러 조각으로 나눠요. map 빌더의 핫 키 자동 분할(ZERO_SPLIT 일반화)과 같은 방식이에요.
- **그 전까지 큰 체인을 돌릴 때:**
  - 메모리 상한은 공식 값의 2.5배로 잡아요.
  - 먼저 `token_stats`에서 이벤트 상위 토큰의 비중을 보고 P를 정해요.
  - 패스마다 RSS와 memory.events(high)를 지켜봐요.

## 2단계에서 고칠 것
### linea `b1459408dcb9bbda`: allow_missing 예외 (core 승인 2026-10-20 15:03 UTC)
- WETH9 extra 3행의 주소가 map 사전에 없어요. 그래서 `--allow-missing`으로 그 3행만 빼고 지었어요.
- **조건:** publish 영수증(PUBLISHED.json)에 exception을 기록해요. verify의 RPC 공급량 대조가 PASS여야 해요.

| block | tx_index | log_index | 종류 | 주소 | 금액 |
|---|---|---|---|---|---|
| 21,699,059 | 4 | 9 | WETH_DEPOSIT | 0x00000000000000304861c3adfb80dd5ebec96325 | 1 wei |
| 21,699,059 | 4 | 10 | WETH_DEPOSIT | 0x00000000000000304861c3adfb80dd5ebec96325 | 1 wei |
| 21,699,059 | 4 | 11 | WETH_WITHDRAWAL | 0x00000000000000304861c3adfb80dd5ebec96325 | 2 wei |

- **순합:** +1 +1 −2 = 0 wei예요. 그래서 공급량은 RPC 값 4,064,994,683,523,339,406,897과 그대로 같아야 해요.
- **고칠 방향:** map 사전을 만들 때 WETH9 extra의 주소처럼 log topic에만 나오는 주소도 넣어요. 그다음 linea를 allow_missing 없이 다시 지어요.

### ledger 입력 폴더의 azcopy 임시 파일
- arbitrum tt 폴더에 OOM으로 죽은 첫 받기의 `.azDownload-*` 조각이 남아 있었어요. 그래서 ledger가 "overlapping block ranges"로 멈췄어요.
- 지금은 그 파일을 지워서 피했어요. ledger_main이 `.azDownload-*`를 무시하게 고치는 건 core 몫이에요.
- `stage_*` 스크립트도 끝날 때 이런 잔재를 지우게 해요.

### hot 크기: 이벤트 예산을 실제로 적용하기 (core 결정 2026-10-20 15:07 UTC)
- robinhood `e265263e29d91ce4` 전체 77.0G 중 hot이 67.9G예요.
  - tt.tok.data 38.4G, bal.head.index 10.2G, bal.top 5.8G, tt.addr.data 4.3G 순이에요.
  - skew 하한(60G) 때문에 오늘은 올리지 않았어요. R1 회수(두 체인의 cards.duckdb 57G) 승인 뒤에 올려요.
- **개선:** ledger의 hot_window와 event_budget_bytes를 실제로 적용해요.
  - 오래된 토큰 이벤트(tt.tok.data 대부분)는 cold 세그먼트로 보내요.
  - fetch의 `--exclude-kinds`로 빼요.
  - 목표: 체인당 hot을 15~20G로 줄여요.

### VM 동시 실행 메모리
- 2026-10-20 15:05에 VM 전체 메모리가 바닥나서 커널이 optimism ledger를 죽였어요. 함께 돌던 것: arbitrum ledger, linea ledger, verify, publish.
- cgroup 상한의 합이 물리 메모리(125G)보다 크면 안 돼요. 큰 ledger는 한 번에 하나만 돌려요.

## 2026-10-21 01:18 KST 갱신 (주인 결정: ETH·Base·Polygon은 graph만 운영)
- **ethereum graph-only `c1ae4fffe3f6ef5d`:** skew에는 hot 42.86G만 받아요(cold 76.98G는 Blob). 16:17 UTC부터 받는 중이에요.
- **Base·Polygon:** graph만 지어요. ledger는 짓지 않아요.
- **VM 재가동 순서 (약 2.5~3시간, 약 $3)**
  1. 준비 약 30분
     - /data RAID를 다시 만들어요(`econ/postpass/vm-setup-e16.sh`).
     - 패키지가 남았는지 확인해요. apt를 쓴다면 `NEEDRESTART_MODE=l`로 돌려요.
     - `tools/sync.sh`로 소스를 보내고, core 최신 바이너리(graph v2, cm2)와 emv2-manifest(새 id 규칙)를 빌드해요.
     - `emv2-diskguard`를 띄워요.
  2. 입력 받기 약 20분: `stage_map_inputs.sh base polygon` (142G + 195G, 같은 지역이라 비용 없음)
  3. graph를 동시에 돌려요
     - base 약 50~60분, polygon 약 80~100분
     - 메모리: 약 33G + 40G
     - 디스크: 입력 338G + 피크 약 350G
  4. **check:** `emv2-graph-tool check`의 메모리가 노드 수에 비례하면 base 약 128G, polygon 약 176G예요. 둘 다 VM(125G)을 넘어요. core가 스트리밍 check를 넣거나, verify 대조로 대신해요(결정 필요).
  5. publish 약 20분: `finish_build.sh`. 그다음 skew는 core의 cold-mode fetcher로 연결해요.

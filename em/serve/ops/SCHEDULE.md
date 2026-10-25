# EMV2 VM 일정표 (ops 관리, 10:30 UTC 기준)

VM은 172.171.247.70 (E16ads_v6, 16 vCPU, 125GB, /data 880G RAID0)이에요. 사용은 **16:15 UTC (01:15 KST)**까지예요.

## 지금 VM에서 도는 작업 (10:30 UTC 실측)
| 유닛 | 담당 | 지금 메모리 / 상한 | 끝 (UTC) |
|---|---|---|---|
| em-poly-sheets8 (exact → revalue → dust → coverage → checks → 업로드) | ac1fa048 | 38G / 46G | exact 약 11:05, 전체 약 11:40 |
| em-muse-polygon (하위 그래프 추출 → glbuild·glserve :8732) | a1e364ee | 32G / 32G | 추출 10:35, glserve 10:42~11:10 (2~3G) |
| em-muse-finish / em-muse-cleanup | a1e364ee | 2.6G / 24G | ~~11:08~~ → **11:26**, 정리와 함께 /data/sub 약 180G를 비워요 |

**사고 기록 (10:25:50 UTC)**
- **무슨 일:** ops가 VM에서 `apt-get install g++-14 liburing-dev`를 돌린 직후, needrestart가 em-muse-polygon을 다시 시작했어요. Polygon 추출이 처음부터 다시 돌아서 약 17분을 잃었어요. em-poly-sheets8은 영향이 없었어요.
- **조치:**
  - Muse 담당이 `/etc/needrestart/conf.d/50-em-no-restart.conf`를 추가했어요. 이제 em-* 유닛은 apt가 자동으로 다시 시작하지 않아요.
  - 앞으로 apt는 `NEEDRESTART_MODE=l`로만 돌리고, 다른 담당 유닛이 돌고 있으면 먼저 알려요.
| Linea·Unichain glbuild | a6b0 | 끝났어요 (10:24) | 입력 /data/gl-inputs 27.5G는 EMV2와 같이 써요 |

- 가용 메모리 87G, /data 여유 584G예요.
- 11:10 뒤에는 Muse가 0G, 11:40 뒤에는 Polygon econ이 0G예요.

## EMV2 빌드 일정 (빌더가 준비된 순서대로)
| 구간 (UTC) | 작업 | 메모리 상한 | 비고 |
|---|---|---|---|
| 빌더 준비 ~ 11:40 | arc → linea → unichain → optimism | 각 16G, optimism 24G | Polygon econ과 동시에 돌려요 (가용 87G) |
| 빌더 준비 ~ 11:40 | robinhood → arbitrum | 24G | 작은 체인 다음, 기존 결과와 대조해요 |
| 11:40 ~ | **polygon** (토큰 이체 두 번 + 그래프) | **64G** | 가장 길어요(설계 추정 약 2시간 40분). 먼저 시작해요 |
| 11:40 ~ | ethereum → base (차례로) | 48G | polygon과 동시에 돌려요. CPU를 나눠 써서 각각 더 걸릴 수 있어요 |
| 끝날 때마다 | emv2_publish.py → Blob | — | 같은 지역이라 업로드는 무료예요. 크기와 MD5 대조, 로컬 sha256 재계산 |
| ~ 16:00 | VM 로컬에만 있는 결과를 Blob으로 옮기고 확인 | — | 그다음 "VM 꺼도 됨"을 보고해요 |

**위험**
- 빌더 코드는 아직 작성 중이에요(core 헤더 목표 11:30). 큰 체인을 13:30 넘어 시작하면 polygon이 16:15 안에 못 끝날 수 있어요.
  - 그때는 polygon을 토큰 이체 1차(버킷 0~127)까지 하고, 중간 산출물을 Blob에 올려 다음 VM 세션에서 이어 가요.
- 메모리 상한: 모든 EMV2 빌드는 `systemd-run --unit emv2-build-<chain> -p MemoryMax=<상한> -p MemoryHigh=<상한-4G> -p OOMScoreAdjust=800 nice -n 10`으로 돌려요.
  - OOMScoreAdjust 800은 Polygon econ보다 먼저 죽게 하려는 거예요(ac1fa048 요청).
  - 11:40 전에는 가용 메모리가 30G 이상일 때만 시작해요.
- Polygon econ 갱신 (10:35): exact 11:05~11:10, checks 11:25, verify 자료 업로드 11:30, ECON_DONE·OWNER_DONE 11:40. 끝나면 /data 약 45G를 비워요(Blob 확인 뒤).
- **Polygon econ 재갱신:** exact 11:35, checks·verify 자료 11:50, **ECON_DONE·OWNER_DONE 약 12:00**이에요.
  - **큰 체인(polygon, ethereum, base) 빌드는 ac1fa048의 OWNER_DONE 알림 뒤에 시작해요.**
  - 그 전에 도는 작은 체인 빌드는 가용 30G 이상과 OOM 800 조건을 지켜요. 지금 Polygon 33G, 가용 92G예요.
  - polygon 약 2시간 40분 + 업로드 → 빌더가 준비돼 있으면 약 14:50에 끝나요. 마감 16:15까지 여유는 약 1시간 20분이에요.

## skew 받기 (디스크 하한 60G)
- **10:31 UTC 여유 80G**예요. 쓸 수 있는 건 약 20G이고, 다른 작업이 분당 약 1.5G씩 쓰고 있어요.
  - em-xc-bringup: Base econ_actions 받기
  - em-evm-rt-bringup: Base sheets
  - gl-fetch: unichain
- **우선순위:** arc → linea → unichain(각 1G 안쪽) → optimism(그래프 11~14G에 잔고·이벤트, 이벤트 예산을 줄여서) → robinhood → arbitrum
- **실제로 들어가는 범위:** 작은 체인 세 개, 그리고 여유가 남으면 optimism 축소판이에요.
- **robinhood·arbitrum·큰 체인:** NVMe가 들어올 때까지 VM과 Blob에서만 검증해요.

## verify (ae4b8527) VM 사용
- 모든 실행은 `systemd-run --scope -p MemoryMax=<상한> -p OOMScoreAdjust=800 nice -n 10`으로 감싸요.
- 체인별 대조: 8G, 한 번에 하나, 빌드가 끝날 때마다 돌려요.
- Polygon focus 대조: 24G. ethereum이나 base가 끝나 가용이 40G 이상일 때 돌리고, 시작 신호는 ops가 줘요.
- robinhood 전체 잔고 대조: 24~32G. 가용 40G 이상인 첫 빈자리에 돌려요. Polygon focus 대조가 먼저이고, 15:30까지 못 돌리면 건너뛰어요.
- verify 자료 업로드: Blob `emv2/verify/20261020/` (약 3.5G)에 15:50까지 올려요. 크기와 MD5를 대조해요.

## build_id 만들기 (core 규약)
1. `sudo python3 /data/emv2-dev/ops/emv2_inputs.py <inputs.json> <blob-prefix>...`
   - Blob 목록에서 md5나 etag를 sha256 칸에 넣어요. TB 단위 입력을 다시 해시하지 않아요.
2. `/data/emv2-dev/opsbuild/build/emv2-manifest build-id <chain> <inputs.json>`
   - 같은 입력이면 같은 id가 나와요. 시험: unichain map/edges → 9b8445c3660e916d가 두 번 같았어요.
3. 빌더에 `--build-id`를 넘겨요. 출력은 `/data/emv2-out/<chain>/<build_id>/`예요.
4. `emv2-manifest merge` → `verify` → `sudo python3 /data/emv2-dev/ops/emv2_publish.py <dir>`
   - publish가 `emv2-manifest verify`를 먼저 돌려요.

## 결정성 빌드 (verify 요청, unichain)
| 판 | 설정 | 출력 경로 | 비고 |
|---|---|---|---|
| A | `EMV2_SIMD=avx2` | `/data/emv2-out/unichain/<build_id>/` | 운영에 쓰는 판이에요. 이 판을 publish해요 |
| B | `EMV2_SIMD=avx512` | `/data/emv2-dev/determinism/unichain-B/<build_id>/` | SIMD 경로 비교용이에요 |
| C | `EMV2_SIMD=avx2` (다시) | `/data/emv2-dev/determinism/unichain-C/<build_id>/` | 같은 경로 두 번 비교용이에요 |

- 세 판 모두 build_id가 같고, 각각 16G 상한과 OOM 800을 걸어요.
- 끝나면 verify가 `emv2/tools/determinism.sh`로 A–C, A–B를 비교해요.

## graph 입력 미리 받기 (10:54 UTC 시작)
| 체인 | 입력 크기 (addresses + edges) | 위치 | 상태 |
|---|---|---|---|
| linea / unichain | 4.3G / 1.3G | /data/gl-inputs 링크 | 준비됨 |
| optimism, robinhood, arbitrum, ethereum | 33 + 13 + 26 + 107G | `/data/emv2-in/<chain>/map/` | `emv2-stage-1`로 받는 중 (2000Mbps 상한, IO idle) |
| base, polygon | 142G, 196G | 같은 위치 | Muse 정리(11:26) 뒤에 받아요. /data 하한 200G를 지켜요 |
| arc | — | graph가 skew에서 복사 (`/data/emv2-dev/graph/in/arc`) | Blob에 arc map이 없어요 |

- graph 빌더 준비 12:00, arc 실행 명령 12:15이에요.
- graph tmp: `/data/emv2-tmp/<chain>`. polygon은 spill 약 63G에 출력의 2배를 잡아요.

## ledger 입력 (full 모드), 10:59 UTC 시작
- **위치:** `/data/emv2-in/<chain>/{tt,blocks}/`. 끝나면 `TT_STAGED` 파일이 생겨요.
- **받는 중:** arc, linea, unichain, optimism, robinhood (`emv2-stage-tt1`, 1500Mbps)
- **나중에:** arbitrum(94G), ethereum(117G)
- **base·polygon:** ledger가 스트리밍으로 받아요(`/data/emv2-stream/<chain>/`, 미리 받는 양 40G 이하). counts·focus 모드로 돌려요.

## /data 디스크 예산 (880G)
- **10:59 여유 485G.** 이후 Muse 정리(11:26)로 +180G, Polygon econ(12:00)으로 +45G가 생겨요.
- **출력이 커요:** ledger는 이벤트당 50~60B예요. arbitrum 약 190G, ethereum 약 250G, optimism 약 120G, robinhood 약 100G예요.
- **그래서 체인을 하나씩 처리해요:** 빌드 → merge·verify → publish → verify 대조 → 로컬 입력과 출력 정리.
  - VM 로컬 사본은 Blob에서 다시 받을 수 있는 임시 사본이에요.
  - 정리하기 전에 Blob 확인(publish 영수증)이 있어야 해요.

## 11:15 UTC 갱신
- **Polygon econ:** exact가 11:55~12:05에 끝나요. ECON_DONE·OWNER_DONE과 verify 자료는 12:20 전후, excluded_pairs는 12:35까지(5G 이하)예요.
  - 큰 체인 64G는 12:20 뒤에 시작해요. 그 전에는 가용 30G 이상과 OOM 800 조건을 지켜요.
- **Muse:** 11:04에 끝났어요(0G). /data/sub에는 30G가 남아 있어요(Blob에 보관된 결과물).
- **빌드 레인:**
  - `emv2-lane-small` (28G): linea → unichain A → B → C
  - `emv2-lane-rh` (60G): robinhood는 graph 단계에서 실패했어요. map edges의 `total_amount_big`이 80자리라 U256을 넘었어요. graph가 고치는 중이에요.
- **map 끝 블록(--max-block, ranges_included.tsv):**

  | 체인 | 끝 블록 |
  |---|---|
  | linea | 32,356,908 |
  | unichain | 61,669,999 |
  | optimism | 158,276,619 |
  | robinhood | 88,098,834 |
  | arbitrum | 512,450,747 |
  | ethereum | 26,199,999 |
  | base | 52,789,326 |
  | polygon | 95,775,112 |
  | arc | 미정 (Blob에 map이 없어요) |
- **build_id:**

  | 체인 | build_id |
  |---|---|
  | linea | 42bbac4f5d6ed302 |
  | unichain | f3d124bd593053ba |
  | optimism | faf50b8025dbe29b |
  | robinhood | e265263e29d91ce4 |
  | arbitrum | 5bb7feeb57232401 |
  | ethereum | c1ae4fffe3f6ef5d |

## 11:25 UTC 상태
- **linea:** BUILD_OK예요(graph 107s, ledger 288s). 크기 29.4GB, 59파일, merge·verify 통과. 지금 Blob에 올리고 있어요.
  - kind별 크기: tt.addr.data 8.9, tt.tok.data 5.6, bal.head.index 5.0, g.edge.exact 3.5, bal.ckpt.data 2.9, g.out.adj 1.0GB, 나머지는 작아요.
  - cold를 빼도 약 22GB예요. 설계 문서가 "작음"으로 잡은 것보다 훨씬 커요.
- **robinhood:** graph 빌더를 고친 뒤(512비트 금액) 다시 돌리는 중이에요(`emv2-lane-rh2`, P=32, 34G 상한).
- **unichain A/B/C:** `emv2-lane-small`에서 linea 다음으로 돌아요.
- **base map 입력:** 받는 중이에요(`emv2-stage-2`, 1000Mbps, IO idle).
- **우선순위:** Polygon econ이 먼저예요. EMV2 유닛은 CPUWeight 20, IOWeight 10이에요.
  - Polygon econ 새 일정: exact 12:35, OWNER_DONE 약 13:00 (`em-poly-after` 로그 `[polygon/after] FINISH_OK`)
- **graph out 형식 개선(약 30% 축소):** 12:00 전에 나와요. 큰 체인 graph는 새 바이너리로 돌리고, 작은 체인은 나중에 다시 빌드해요.
- **skew 디스크:** 여유 58G로 이미 하한(60G) 아래예요. 지금은 어느 체인도 skew로 받을 수 없어요.

## 11:58 UTC 갱신
- **unichain A** (`f3d124bd593053ba`, graph v2 + ledger 형식 2): Blob에 올렸어요(56객체, 9.23GB, 11:58). EMV2로 처음 올린 체인이에요.
- **robinhood:** 11:48에 ledger가 34G 상한에서 OOM으로 죽었어요(anon 35.5G). 그래서 60G 상한 레인으로 옮겼어요.
- **`emv2-lane-main` (60G):** linea(12:45 목표) → robinhood → unichain B → unichain C 순서로 돌아요.
- **arc:** core 결정(b)로 가요. Blob realtime/arc를 VM 스냅숏으로 얼리고(`emv2-arc-snap`) → build_map(19개 checks) → 같은 build_id로 graph·ledger를 지어요(`rebuild_arc.sh`). 13:30까지 checks를 통과하지 못하면 1단계에서 빼요.
- **입력 시점 확인:**
  - robinhood, arbitrum, unichain의 tt는 map을 지은 뒤 바뀐 파일이 0개예요.
  - linea, optimism, ethereum의 tt는 2026-10-19 20:12~20:14 UTC에 모두 다시 올라갔어요(map 뒤). linea는 verify 대조로 내용이 같다는 걸 확인했고, optimism과 ethereum은 counts로 확인해야 해요.
- **polygon/extraction Cold 이동:** EMV2 polygon 빌드와 verify의 직접 읽기 대조(남은 364파일)가 끝날 때까지 미뤄요. 끝나면 ops가 컨트롤러에게 알려요.

## 12:06 UTC 결정 (ledger)
- **ethereum·base·polygon은 1단계에서 graph만 publish해요.** 빌드 폴더에 manifest.ledger.json을 넣지 않아요. 그러면 serve가 장부 경로를 "준비 중"으로 답해요.
  - 실행: `run_graph.sh` (GRAPH_BIN=cm2) → `finish_build.sh` (merge → verify → publish)
- **counts·focus는 검증 자료로만 써요.**
  - base_counts.tsv, eth_counts.tsv: verify 9/9 통과
  - polygon bal_focus.tsv: 12:02 시작, 약 25분
- **arbitrum full:** robinhood를 publish하고 로컬 사본을 정리한 뒤에 돌려요. P=32, per-pass 2, 약 31G, 출력 약 130G예요.
- **ethereum full:** 디스크가 남을 때만 해요. 아니면 2단계로 넘겨요.

## 12:30 UTC 갱신
- **linea v2** `42bbac4f5d6ed302`: Blob에 올렸어요(12:19, 92객체, 18.55GB). 11객체는 v1과 내용이 같아서 다시 썼어요(dict.addr 등).
  - **주의:** 같은 build_id라 v1 manifest.json과 PUBLISHED.json은 Blob에서 v2로 덮어써졌어요(옛 사본은 남기지 못했어요). v1 전용 객체 약 48개는 고아예요.
- **arc:** 얼린 스냅숏으로 build_map을 다시 돌려서 checks를 통과했어요. graph v2 `627c60285101f829`(노드 336만, 원래 행 3,071만)이 나왔고, ledger를 짓는 중이에요.
- **build_id(graph만):** base `0c82116ac8942e8f`(map 142G), polygon `2830f8af368aa0f0`(map 195G)
  - ethereum `c1ae4fffe3f6ef5d`는 tt·blocks 입력까지 넣어 만든 id예요(graph만 publish해요).
- **VM 디스크:** 12:20에 여유 70G까지 줄었어요. Muse /data/sub(30G, 담당 승인)와 실패한 arc 폴더를 지워서 97G가 됐어요. verify(86G)에는 정리를 요청했어요.
- **CPU:** load 65~68이에요. linea와 arc는 200, robinhood·eth는 100, polygon focus는 50으로 맞췄어요.

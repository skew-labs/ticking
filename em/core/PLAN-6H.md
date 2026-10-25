# EMV2 1단계: 6시간 실행 계획 (2026-10-20)

**주인 승인 (19:25 KST)**
1. skew NVMe 추가. 새 엔진 검증 뒤 기존 사본 약 250GB 회수.
2. cold 계층 첫 조회 ≤ 300ms 허용.
3. 1단계를 **6시간 안에** 끝내고, 에이전트 수 제한은 없어요.
4. 운영 브리지 shadow 플래그 배포. 기본값은 `old`예요.
5. Blob +2.6TB.

**마감:** 16:15 UTC (01:15 KST). 설계 문서는 `/Users/huhyul/em-mapping/design/EM-ENGINE-V2.md`예요. 형식과 목표는 그 문서가 기준이에요.

## 6시간 안의 완료 정의
- **코드:** `machine::engine` 기초, CSR-T 빌더, tt·bal 빌더, emv2-serve(wallet, holders-at, wallet-trades, acquisition, address-chains, neighbors, trace, path)
  - econ-machine ctest와 함께 통과해야 해요.
  - ASan/UBSan과 결정성(두 번 빌드한 바이트가 같음)도 통과해야 해요.
- **빌드:** 9개 체인 모두 VM에서 빌드하고 Blob `<chain>/emv2/`에 sha256 manifest와 함께 올려요.
  - arc, linea, unichain, optimism, arbitrum, robinhood, ethereum, base, polygon
- **skew 서빙:** 디스크 하한(여유 60GB) 안에 들어가는 체인만 올려요. 우선순위는 arc → linea → unichain → optimism → robinhood → arbitrum이에요.
  - 큰 체인 세 개는 NVMe가 들어올 때까지 VM과 Blob에서만 검증해요.
- **검증:**
  - CASHCAT 골든 케이스
  - Base 재검사 14항목
  - Polygon focus_balances 2,103만 쌍의 정확값 일치(부호 뒤집힘 106,829쌍 → 0)
  - Arbitrum·Robinhood는 기존 결과와 행 단위로 대조
- **브리지:** 경로별 `old | shadow | new` 플래그를 넣어요.
  - staging에서 shadow로 비교 로그가 쌓이는지 확인해요.
  - 운영은 플래그를 넣고 기본값 `old`로 둬요. check_prod 전후가 같아야 해요.
- **남는 것:** 7일·1만 요청 이중 실행 뒤 `new`로 전환하는 일은 6시간 밖이에요. 시작만 해 둬요.

## 코드 위치와 담당 (파일 단위로 나눠서 충돌을 막아요)
- **저장소:** `/Users/huhyul/em-mapping/emv2/`
  - Mac이 원본이에요. 빌드와 시험은 Linux(skew `/srv/skew/emv2-dev/<담당>/`, VM `/data/emv2-dev/`)에서 해요.
  - rsync로 원본 전체를 자기 폴더에 보내요.
- **바깥 코드:** `/Users/huhyul/em-mapping/vendor/economic-machine/native`(machine_economics), `/Users/huhyul/em-mapping/graphloop/include/machine/graphloop`
  - 둘 다 수정하지 않아요. 바꿀 게 있으면 emv2 안에 감싸는 코드를 둬요.

| 담당 | 맡는 파일 |
|---|---|
| **core** (총괄 겸 인터페이스) | `CMakeLists.txt`, `include/machine/engine/{u256,codec,dict,seg,ring}.hpp`, `include/machine/engine/api.hpp`(모든 모듈의 선언), `tests/core_*.cpp` |
| **graph** | `include/machine/engine/graph_v2.hpp`, `src/build/graph*.cpp`, `tests/graph_*.cpp` |
| **ledger** | `include/machine/engine/{ledger,prices}.hpp`, `src/build/{tt,bal}*.cpp`, `tests/ledger_*.cpp` |
| **serve** | `src/serve/*.cpp`, `include/machine/engine/serve*.hpp`, `tests/serve_*.cpp` |
| **bridge** | 로컬 브리지 소스(em_bridge.py 등)의 shadow 플래그, staging 적용, 운영 적용(adc의 2_bridge_apply/rollback 방식) |
| **verify** | `verify/`(대조 하네스, 골든 케이스, 벤치), `bench/` |
| **ops** | `ops/`(VM 빌드 오케스트레이션, Blob 업로드·manifest, skew 내려받기·SIGHUP, systemd 유닛), VM 메모리 일정, NVMe 슬롯 조사 |

- **인터페이스 규칙:** core가 `api.hpp`와 `u256/codec/dict/seg` 첫 판을 **20:30 KST(11:30 UTC)**까지 내요.
  - 그 전에는 다른 담당이 설계 문서 5절의 형식대로 자기 쪽 헤더를 먼저 써요.
  - core 쪽이 필요하면 `// TODO(core)`로 남기고 core에 SendMessage로 알려요.
  - 남의 파일은 고치지 말고 그 담당에게 요청해요.

## 중간 목표 (UTC)
| 시각 | 목표 |
|---|---|
| 11:30 | core 헤더와 CMake 첫 판. 모든 담당이 skew에서 컴파일돼요 |
| 12:45 | 작은 체인(arc) 빌드 → Blob → skew, emv2-serve가 응답, staging 브리지 shadow 비교 시작 |
| 13:45 | linea, unichain, optimism, robinhood, arbitrum 빌드와 대조. CASHCAT 통과 |
| 15:30 | ethereum, base, polygon VM 빌드와 검증. Base 14항목, Polygon focus_balances |
| 16:00 | 운영 브리지 플래그 배포(기본 old), check_prod 전후 비교 |
| 16:15 | 최종 보고 |

## VM (172.171.247.70, E16ads_v6, 125GB RAM, /data NVMe RAID0)
- 22:00 KST 종료 계획은 취소하고 01:15 KST까지 써요. 주인에게 알렸어요.
- **메모리 일정은 ops가 정해요.**
  - 지금: Polygon econ(약 20:30 KST까지, 86G 상한), Muse, Linea·Unichain glbuild(24G)가 돌고 있어요.
  - 그 뒤: EMV2 빌드에 64G 예산을 써요.
  - 다른 작업 담당(ac1fa048, a1e364ee, a6b02426155e2d80c)과 SendMessage로 맞춰요.
- **VM 로컬에만 있는 결과는 마감 전에 Blob으로 옮겨요.**

## skew 규칙 (운영 서버예요)
- 가용 메모리 하한 6GB, 디스크 여유 하한 60GB예요.
- 빌드와 시험은 `nice -n 19 ionice -c3`로 돌리고, `--parallel 4` 이하로 해요.
- 차트(em-map-charts*)와 운영 Ticking 서비스와 데이터는 건드리지 말아요.
- 새 상시 서비스(emv2-serve)는 MemoryHigh/Max, OOMScoreAdjust 600을 걸어요.
  - 주인이 1단계를 승인했으니 emv2-serve 유닛은 등록해도 돼요.
  - 다른 타이머나 상시 작업은 컨트롤러에게 먼저 물어요.

## 담당 ID (SendMessage 주소)
| 담당 | ID |
|---|---|
| core·총괄 | a2cb6ce10fe1146dc |
| graph | a7bee2ea0c13640dc |
| ledger | aee2a425be1aa6b39 |
| serve | aeb55d789b0cbe6ed |
| bridge | a930fbbf274b1377f |
| verify | ae4b85272929bc69a |
| ops | a32e74c74aa6a6989 |

**EMV2 밖 작업**

| 작업 | ID |
|---|---|
| Polygon econ | ac1fa048fbe3f9f4e |
| Muse 라벨링 | a1e364ee39930a4c3 |
| 새 체인 glserve·브리지 표 | a6b02426155e2d80c |
| UI 릴리스(브리지 원본 동기화) | afac64654c71a16b5 |
| 로고 | a0682c1e8493f863b |
| 리서치 허브 | a81ef2145a6040f57 |
| Blob 삭제 목록 | a1b7a60207b38236a |

## 공통 규칙
- SAS, 키, URL 쿼리는 출력하거나 기록하지 말아요.
- 운영 Ask에 시험 질문을 보내지 말아요.
- Blob과 skew에서 삭제가 필요하면 목록을 만들어 컨트롤러에게 보내요. 지우지는 말아요.
- 보고는 한국어로 해요. 짧게, 숫자와 경로로 써요.

# EMV2 브리지 shadow (bridge 담당, 2026-10-20)

브리지(em_bridge.py)에 경로별·체인별 `old | shadow | new` 스위치를 넣는 별도 패처예요. em_bridge.py 원본 줄은 고치지 않아요. 설계 기준은 `design/EM-ENGINE-V2.md` 8절(API)과 10절(이중 실행)이에요.

## 파일 (Mac 원본 → skew `/srv/skew/emv2/bridge/`)
| 파일 | 하는 일 |
|---|---|
| `emv2_block.py` | 브리지에 들어가는 블록이에요. Handler.api를 감싸요 |
| `apply_bridge_emv2.py` | 블록 삽입 패처예요. 앵커는 `if __name__ == "__main__":` 하나예요. 멱등이고, 예전 블록이 있으면 통째로 바꿔요 |
| `emv2-bridge.staging.json` | staging 설정 예시예요(linea 경로만 shadow, 표본 0.5) |
| `emv2_shadow_report.py` | 경로·체인별 요약과 전환 판정(1만 요청·7일·치명적 차이 0·V2 실패 ≤ 0.1%) |
| `emv2_shadow_replay.py` | staging에 보내는 시험 트래픽이에요. 체인 하나에 대해 마켓 → 보유자 → 지갑 경로를 초당 2건 이하로 GET해요 |
| `emv2_route_snapshot.py` | 운영 적용 전후에 EM 경로의 상태 코드와 키를 비교해요 |
| `emv2_prod_apply.sh` / `emv2_prod_rollback.sh` | 운영 브리지에 블록만 넣고 빼요 |
| `test_emv2_bridge.py` | unit(비교 규칙, 경로·체인, 모드) / live(시험 사본 :8749 + 모의 V2 :8748) |

## 대상 경로 (route 이름 → 공개 경로)
- `holders-at`, `wallet-trades`, `acquisition`, `address-chains`(체인 `*`), `trace-graph` → invscreen 경로
- `trace`: `/api/public/em/trace`, to가 없을 때
- `path`: 같은 경로, to가 있을 때(glserve /tools/path)
- `neighbors`: `/api/public/em/g/neighbors/<chain>/<id>`
- `tools/wallet`, `tools/holders`, `tools/trace`

체인은 `chain` 쿼리에서 읽고, 없으면 `market`의 앞부분(`arc:0x…`)에서 읽어요.

## 설정 (`<브리지 폴더>/emv2-bridge.json`, 30초마다 다시 읽고 재시작은 필요 없어요)
```json
{"serve": "127.0.0.1:8740", "default": "old",
 "routes": {"holders-at": {"arc": "shadow", "*": "old"}, "trace": "shadow"},
 "sample": 0.5, "min_gap_ms": 200, "timeout_ms": 2000, "queue": 64, "log_max_mb": 64, "log_keep": 5, "align_old": true}
```
- **파일이 없으면 모든 경로가 old예요.** 블록이 없는 브리지와 같은 길로 답해요.
- **shadow:** 기존 응답을 먼저 보내요. 그다음 스레드 하나가 같은 path·query로 emv2-serve에 물어보고 비교해서 `<--logs>/emv2-shadow.jsonl`에 남겨요. 로그는 크기로 회전해요.
  - 큐가 차면 그 요청은 비교하지 않고 버려요(`dropped`).
  - V2가 죽거나 느려도 응답에는 영향이 없어요.
- **new:** emv2-serve가 답해요. 실패(200이 아님, JSON 아님, 연결 오류)하면 그 요청만 old로 답해요(`new_fallback`).
- **비교 규칙:**
  - 정수와 문자열은 완전히 같아야 해요.
  - 그 밖의 숫자는 상대오차 1e-6 이하예요.
  - 리스트는 순서와 길이까지 같아야 해요.
  - 시각과 로고 필드는 비교하지 않아요(`EMV2_IGNORE`).
  - Pro 게이트로 가려진 응답(`locked`)은 비교하지 않아요.
- **시점 맞추기:** V2 1단계는 map 끝 블록까지예요. 운영은 거기에 매일 delta를 더해요. V2 응답의 `as_of`(블록·시각), 없으면 헤더 `X-EMV2-As-Of: <block>:<time>`를 기준으로 맞춰요.
  - holders-at(`block`)과 trace-graph(`to`)는 기존 upstream에 같은 시점으로 다시 물어서 비교해요(`align: param`). `align_old: false`면 이 재질의를 하지 않아요.
  - 나머지 경로는 기존 응답 리스트에서 as_of 뒤의 행(블록이나 시각 필드)을 빼고 비교해요(`align: trim`).
  - 이렇게 뺀 행이 있는데도 남는 차이는 `time_skew`로 따로 세요. 치명적 차이로는 치지 않아요.
  - shadow는 동시 1개(스레드 하나), 시간 상한 2초로 해요(skew IO 압력).
- **판정 종류:**
  - `match`: 같아요
  - `time_skew`: as_of 뒤 활동으로 설명되는 차이예요
  - `critical`: 상태 코드, 값, 타입, 길이, 빠진 필드 중 하나가 달라요
  - `unsupported`: V2가 501을 줬어요
  - `v2_budget`: V2가 504를 줬어요
  - `v2_down`: V2 연결이 안 돼요
  - `v2_only`: 기존은 400/404, V2만 답해요
  - `old_failed`: 기존 upstream이 5xx였어요
  - `same_status`: 둘 다 같은 오류 코드예요
  - `old_unavailable`: 기존 서비스가 available:false("준비 중")로 답해서 비교하지 않았어요

## 명령
```sh
# staging (런처가 flowmap → account → emv2 순서로 다시 만들어요)
sudo bash /srv/skew/ticking-em-staging/fc/src/staging/run-bridge-flowmap.sh
install -m 644 emv2-bridge.staging.json /srv/skew/ticking-em-staging/fc/bridge-staging/emv2-bridge.json   # 켜기
rm /srv/skew/ticking-em-staging/fc/bridge-staging/emv2-bridge.json                                         # 끄기 (30초 안)
python3 emv2_shadow_replay.py arc --rps 2
python3 emv2_shadow_report.py /srv/skew/ticking-em-staging/fc/logs/bridge

# 운영 (블록만 넣어요. 설정 파일이 없으니 모두 old예요)
bash emv2_prod_apply.sh --dry-run
bash emv2_prod_apply.sh         # check_prod와 EM 경로 전후 비교. 바뀌면 자동으로 되돌려요
bash emv2_prod_rollback.sh      # backups/LAST-EMV2
python3 emv2_shadow_report.py /srv/skew/em-ticking-bridge/logs
```
- 운영 shadow 켜기는 `/srv/skew/em-ticking-bridge/emv2-bridge.json`(root 0644)이에요. staging 비교가 깨끗해야 하고, 컨트롤러 확인을 받은 뒤에만 켜요.

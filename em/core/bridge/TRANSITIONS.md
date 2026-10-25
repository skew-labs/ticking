# EMV2 운영 전환 기록 (운영 브리지 em-ticking-bridge :8896, 설정 /srv/skew/em-ticking-bridge/emv2-bridge.json)

기준(FINAL-PRODUCTION-PLAN A.4): 경로별 verify PASS + shadow 1,000건 치명적 차이 0(time_skew 제외). V2 전용 경로와, 기존
경로가 없는(400/502) 체인은 비교 대상이 없어 verify PASS만으로 판정해요. 롤백: 설정에서 그 줄을 지우거나 "old"로 바꾸면 30초 안에 돌아가요.

| 시각 (UTC) | 경로 · 체인 | old → new | 판정 근거 | 확인 |
|---|---|---|---|---|
| 2026-10-20 14:15 | (블록 설치) | 전 경로 old | emv2_prod_apply.sh, EM 경로 snapshot unchanged, check_prod no change | 백업 backups/emv2-20261020-141521 |
| 2026-10-20 14:28 | accumulators, tools/accumulators (V2 전용) | 404 → 200 | verify PASS(arc AUSD 7d·24h 상위 10 deltaRaw·순서 정확 일치, 원본 재계산) | 운영 :8896 200 0.11s, ticking.dev 200 0.93s, check_prod no change |
| 2026-10-20 14:29 | holders-at · arc | 502 → 200 | verify PASS(USDC block 26,000,000 holders 356,706·상위 30, snaps 6시점 일치) | 운영 200 0.03s, robinhood holders-at(old) 200 그대로, check_prod no change |
| 2026-10-20 14:34 | tools/holders · arc (Ask 카드) | 404 → 200 | holders-at과 같은 엔진·같은 PASS 데이터(상위 15, 라벨 overlay) | 운영 200, check_prod no change. robinhood tools/holders(old)는 그대로 200 |
| 2026-10-20 15:03 | holders-at · unichain, tools/holders · unichain | 502/404 → 200 | verify PASS(unichain A: 잔고 28,384행·합계 237,628,253, neighbors·address-chains PASS) | 운영 200, check_prod no change. 시점(t/block/snaps) 없는 holders-at은 기존과 같이 오류(V2 400 → old 502) |
| 2026-10-20 15:55 | (블록 갱신: graph, featured 경로 추가) | 경로 old 유지 | emv2_prod_apply.sh --keep-config: 블록만 교체, EM 경로 snapshot unchanged, check_prod no change | 백업 backups/emv2-20261020-155538 |
| 2026-10-20 15:57 | graph (V2 전용, window=all만), featured (정적 스냅숏) | 404 → 200 | verify PASS: hop-1 exact 엣지 arc 858·unichain 563개가 map edges와 n·amountRaw·블록 일치, 노드 라벨·isContract 일치, hop-2·USD PASS. window≠all은 브리지에서 400(전체 기간 합계라 FAIL 판정) | 운영 graph 1홉 0.36 s(브리지 xc 병합 포함), featured 0.7 ms; ticking.dev 200; check_prod no change. 1홉 p99 arc 127 ms(목표 10 ms 초과, 기록) |
| 2026-10-20 15:59 | holders-at · linea, tools/holders · linea | 400 → 200 | verify PASS(linea b145: WETH9 제외 76,869,971쌍 전부 일치, WETH9 공급 = RPC − 0x0 전송 910건) | 첫 check_prod compare가 "CHANGES FOUND" → 30초 안에 롤백(15:59:30), 롤백 뒤 no change. 새 baseline으로 다시 켠 뒤(16:00) compare no change → 일시 변동으로 판단, 유지 |
| 2026-10-20 16:49 | (차단) ethereum·arbitrum graph | 노출 → 501 | 16:38·16:43 ops fetch가 `current`를 만들고 HUP. 브리지 graph가 전 체인 `new`라 검증 전 graph가 노출됨(ETH caps 5, arbitrum caps 37) | `current`→`staged`, HUP. 첫 compare는 market 502→200(16:48:41 ticking-console 재시작, 무관), 새 baseline compare no change. checks/compare-20261020T164821Z.txt, compare2-… |
| 2026-10-20 16:54 | emv2-serve 바이너리 교체 (held 목록, /tokens, /capabilities, 1홉 부분 스캔, 응답 캐시) | 같은 경로 | 운영↔새 바이너리 대조: graph 88건·holders/accumulators 300건 동일(다른 3건은 운영 504 budget, 새 쪽 200). 1홉 warm p99 86.5 → 16.6 ms | check_prod no change(checks/compare-20261020T165356Z.txt). 이전 바이너리 bin/emv2-serve.prev-20261020T165356Z |
| 2026-10-20 16:54 | held 14개 토큰 (arc 3, unichain 7, linea 4) | 숫자 → available:false | 독립 RPC 대조(tools/rpc_truth.py, 상위 20 토큰 × 20 보유자, 0.1%) FAIL: arc USDC(네이티브), 가짜 USDC(balanceOf revert), 네이티브 의사 토큰(가스비 0.24%), 스팸 9, LVC(비표준), z0ETH(이자 토큰) | held.tsv. 운영 tools/holders arc USDC → held 답 확인 |
| 2026-10-20 17:02 | emv2-serve 바이너리 (스팸 분류, 기호 색인) | 같은 경로 | 스팸: held.tsv class(spam·revert) 또는 이름 규칙(URL·claim·visit·airdrop 등) → holders/accumulators held 답(spam:true), graph 1홉 자산에서 제외(spamEdgesHidden), /tokens에서 맨 뒤 + spam 표시. 이자·비표준 토큰은 balanceKind event_sum. /tokens 1.0 s → 2~28 ms | check_prod no change(checks/compare-20261020T170259Z.txt) |
| 2026-10-20 17:03 | 브리지 EMV2 블록 교체 (--keep-config) | 경로 그대로 | held 토큰의 tools/accumulators 카드 message = 이유(“매수자 없음” 아님), held·reasonKo 전달. /tokens·/capabilities를 EMV2 전용 경로로 추가 | emv2_prod_apply: EM routes unchanged, check_prod no change(checks/bridge-apply-20261020T170353Z.txt). 백업 backups/emv2-20261020-170353 |
| 2026-10-20 17:04 | tokens, capabilities (V2 전용) | 404 → 200 | 기호 → 토큰(보유자 순, 스팸 맨 뒤), 체인별 도구·asOf·held 목록 | config backups/emv2-bridge.json.20261020T170439Z, check_prod no change(checks/compare-20261020T170439Z.txt) |
| 2026-10-20 17:25 | emv2-serve 바이너리 (holders-at 시점 병렬, cold 일괄 미리받기) | 같은 경로 | snaps 6시점을 병렬로 읽어요. warm 94~120 ms → 1~47 ms. cold 미리받기는 cold 파일이 없는 운영 체인에서는 아무 일도 안 해요 | check_prod no change(checks/compare-20261020T172457Z.txt) |
| 2026-10-20 17:38 | 브리지 EMV2 블록 (xc 대기 120 ms) | 경로 그대로 | graph 첫 조회의 xc 병합(em-xc wallet-xc)이 200 ms~1.1 s를 더했어요. 이제 120 ms만 기다리고, 늦으면 xcPending:true(no-store)로 답한 뒤 백그라운드에서 캐시해요 | emv2_prod_apply: EM routes unchanged, check_prod no change(checks/bridge-apply-20261020T173849Z.txt) |
| 2026-10-20 17:44 | emv2-serve 바이너리 (모양 비슷한 스팸, graph-only 자산 주소, edgesHidden) | 같은 경로 | 키릴·리수·전각·hair space로 주요 기호를 흉내 낸 토큰은 스팸으로 처리해요(USD₮0 같은 정상 기호는 그대로). 토큰 사전이 없는 빌드는 자산 주소를 map에서, symbol은 overlay에서 채워요. 한도를 넘어 숨긴 (상대, 자산) 행 수는 edgesHidden에 넣어요 | check_prod no change(checks/compare-20261020T174407Z.txt) |
| 2026-10-20 17:50 | emv2-serve 바이너리 + stables.tsv (정식 스테이블 목록) | 같은 경로 | USD 1:1은 목록에 있는 체인(arbitrum·arc·unichain·linea)에서 정식 컨트랙트에만 붙여요. 같은 기호를 쓰는 다른 컨트랙트는 사칭 스팸으로 처리해요. 흉내 규칙에 Cherokee 문자, 앞뒤 공백, 와일드카드를 추가했어요. verify arbitrum 41건에서 스팸 PASS(남은 흉내 0, USD는 정식 5개뿐) | check_prod no change(checks/compare-20261020T175013Z.txt) |
| 2026-10-20 17:53 | emv2-serve 바이너리 (graph 창 합계, 결합 문자 스팸) | 같은 경로 | window=24h/7d/30d면 1홉 토큰 흐름을 ledger 주소 이벤트로 정확히 합쳐요(totalsScope "window", native 제외 note). 대조: unichain USDC 7d accumulators 상위 10명 중 9명의 순변화가 graph 창 흐름 합과 같아요. 남은 1명은 edgesHidden 1,855로 한도 밖이었어요. ledger가 없는 체인은 400 window_totals_preparing | check_prod no change(checks/compare-20261020T175359Z.txt) |
| 2026-10-20 17:57 | 브리지 EMV2 블록 (graph 창을 엔진으로 넘김) + graph window 7d·30d (arc·unichain·linea) | 400 → 200 | 브리지의 일괄 400을 빼고, 엔진 답(창 합계 또는 400 reason)을 그대로 넘겨요 | emv2_prod_apply: EM routes unchanged, check_prod no change(checks/bridge-apply-20261020T175704Z.txt) |
| 2026-10-20 18:04 | emv2-serve 바이너리 (path: 허브 표시, hot 차수) | path는 여전히 old | 허브 판정을 hot 목록 차수로 바꿨어요(cold detail 읽기 없음). 중간 노드는 hot 상위 256만 써요. 못 찾으면 onlyViaHubs·blockedByHub를 줘요. 15쌍 중 12쌍이 glserve와 같고, 다른 3쌍은 모두 차수 2만을 넘는 허브를 거쳐야만 이어지는 경로예요. arbitrum warm 1.7 s → 5~9 ms | check_prod no change(checks/compare-20261020T180415Z.txt) |

## old 유지 (이유)
- path(trace + to): 14:53 수정 뒤 지연은 통과(cold p99 174 ms, warm p99 3.8 ms)했지만 **정확도 FAIL**. glserve /tools/path와 10쌍 중 8쌍만 같고, 2쌍은 V2가 허브의 hot 상위 256만 보느라 경로를 놓쳐요(3홉, 5홉). 허브 끝점은 full 목록으로 펼치고, 못 찾으면 "허브에서 끊김(불완전)"으로 답하게 고친 뒤 다시 재요.
- trace-graph: in 2홉 cold p99 804 ms, warm p99 60 ms(이상치 1~2건). 기존(invscreen cold p50 3.8 s)보다는 빠르지만 목표 밖이고, 링크 구성 비교가 끝나지 않았어요.
- trace(to 없음): trace-graph와 같은 엔진이라 같이 판단해요.
- neighbors(/g/neighbors/<chain>/<id>): 경로의 id가 galaxy node id라 V2 id와 달라요. id 변환을 넣은 뒤 shadow해요.
- address-chains("*"): V2가 서빙하는 체인이 일부(arc)라 바꾸면 체인이 빠져요. 9개 체인이 다 올라온 뒤 판단해요.
- holders-at · arbitrum/robinhood 등: V2 빌드가 skew에 올라오면 shadow 1,000건 뒤 판단해요. robinhood e265(verify PASS, CASHCAT 16/16)는 hot 67.9G라 skew 하한(60G) 때문에 R1 회수 뒤에 올려요.

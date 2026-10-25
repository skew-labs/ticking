# EMV2 1단계 검증 보고 (verify)

기준값: VM `/data/emv2-dev/verify/golden/`, Blob `emv2/verify/20261020/golden/`. map 표본(체인당 주소 약 1,900개)과 checks.json, CASHCAT, Base RECHECK, Polygon 정확값.

## 체인별 결과

| 체인 | 행 대조 (edges·nodes·bal·token_stats) | 체인 합계 (checks 등식) | 실패 항목 | time_skew 행 |
|---|---|---|---|---|
| arc | PASS | PASS | - | 0 |
| linea | PASS | PASS | - | - |
| unichain | PASS | PASS | - | 0 |
| optimism | PASS | PASS | - | - |
| robinhood | PASS | PASS | - | 0 |
| arbitrum | PASS | PASS | - | - |
| ethereum | PASS | PASS | - | - |
| base | 미실행 | 미실행 | - | - |
| polygon | 미실행 | 미실행 | - | - |

### arc

- PASS edges: keys present in map but missing in V2 {"differing": 0, "compared": 1739820}
- PASS edges: keys in V2 but not in map {"differing": 0, "compared": 1739820}
- PASS edges: n, total_amount, first/last block equal {"differing": 0, "compared": null}
- PASS nodes: sampled addresses present {"differing": 0, "compared": 1905}
- PASS nodes: wallet-card counters equal {"differing": 0, "compared": null}
- PASS bal: map (address, token, kind) rows missing in V2 {"differing": 0, "compared": 269256}
- PASS bal: V2 rows not in map {"differing": 0, "compared": 269256}
- PASS bal: n_in, n_out, first/last block equal {"differing": 0, "compared": null}
- PASS bal: holds (exact sign in the map) == V2 balance > 0 {"differing": 0, "compared": null}
- PASS token_stats: n_transfers equal per token {"differing": 0, "compared": 300291}
- PASS token_stats: tokens missing in V2 {"differing": 0, "compared": null}
- PASS counts: sum(n_in) == all token transfers {"got": "130839657", "expected": "130839657"}
- PASS counts: sum(n_out) == all token transfers {"got": "130839657", "expected": "130839657"}
- 측정: map net_approx(double) vs V2 정수 잔고 {"n": 268672, "approx_sign_differs": 0, "max_rel_err": 0.00035583145700219756}
- V2 bal의 erc1155 포함: True
- PASS 합계 native_tx edges.n == count(tx with to): 57908754 / 기대 57908754
- PASS 합계 erc20 edges.n == token_transfers ERC20(+WETH): 126115271 / 기대 126115271
- PASS 합계 erc721 edges.n == token_transfers ERC721: 4702245 / 기대 4702245
- PASS 합계 erc1155 edges.n == token_transfers ERC1155: 22141 / 기대 22141
- PASS 합계 sum(addresses.tx_sent) == count(tx): 57969413 / 기대 57969413
- PASS 합계 sum(addresses.tx_received) == count(tx with to): 57908754 / 기대 57908754
- PASS 합계 sum(token_transfers_out) == all token transfers: 130839657 / 기대 130839657
- PASS 합계 sum(token_transfers_in) == all token transfers: 130839657 / 기대 130839657
- PASS 합계 native edges total_amount == raw recompute (hex path): 167376962260152422261049390 / 기대 167376962260152422261049390
- PASS 합계 is_contract count == contract_creations rows (distinct): 60506 / 기대 60506
- PASS 합계 addresses unique: 3363574 / 기대 3363574
- PASS 합계 ledger sum_n_in == all token transfers: 130839657 / 기대 130839657
- PASS 합계 ledger sum_n_out == all token transfers: 130839657 / 기대 130839657
- PASS 합계 edge rows == map edge rows (map got, informational): 30708112 / 기대 None
- V2가 다시 계산하지 않는 등식: duplicate (from,to,asset,kind) keys, sum(token_stats.n_transfers) == all token transfers, sum(address_tokens.n_in) == all token transfers, sum(address_tokens.n_out) == all token transfers, native edges total_amount == addresses.native_value_received, sum(contracts_created) == contract_creations rows, sum(logs_emitted) == logs rows, unknown token standards (skipped)

### linea

- PASS edges: keys present in map but missing in V2 {"differing": 0, "compared": 1800107}
- PASS edges: keys in V2 but not in map {"differing": 0, "compared": 1800107}
- PASS edges: n, total_amount, first/last block equal {"differing": 0, "compared": null}
- PASS nodes: sampled addresses present {"differing": 0, "compared": 1905}
- PASS nodes: wallet-card counters equal {"differing": 0, "compared": null}
- PASS 합계 native_tx edges.n == count(tx with to): 304200469 / 기대 304200469
- PASS 합계 erc20 edges.n == token_transfers ERC20(+WETH): 225266960 / 기대 225266960
- PASS 합계 erc721 edges.n == token_transfers ERC721: 56604026 / 기대 56604026
- PASS 합계 erc1155 edges.n == token_transfers ERC1155: 16413510 / 기대 16413510
- PASS 합계 sum(addresses.tx_sent) == count(tx): 304721480 / 기대 304721480
- PASS 합계 sum(addresses.tx_received) == count(tx with to): 304200469 / 기대 304200469
- PASS 합계 sum(token_transfers_out) == all token transfers: 298284496 / 기대 298284496
- PASS 합계 sum(token_transfers_in) == all token transfers: 298284496 / 기대 298284496
- PASS 합계 native edges total_amount == raw recompute (hex path): 14103686276204799759567957 / 기대 14103686276204799759567957
- PASS 합계 is_contract count == contract_creations rows (distinct): 514182 / 기대 514182
- PASS 합계 addresses unique: 11543803 / 기대 11543803
- PASS 합계 edge rows == map edge rows (map got, informational): 227492411 / 기대 None
- V2가 다시 계산하지 않는 등식: duplicate (from,to,asset,kind) keys, sum(token_stats.n_transfers) == all token transfers, sum(address_tokens.n_in) == all token transfers, sum(address_tokens.n_out) == all token transfers, native edges total_amount == addresses.native_value_received, sum(contracts_created) == contract_creations rows, sum(logs_emitted) == logs rows, unknown token standards (skipped)

### unichain

- PASS edges: keys present in map but missing in V2 {"differing": 0, "compared": 279198}
- PASS edges: keys in V2 but not in map {"differing": 0, "compared": 279198}
- PASS edges: n, total_amount, first/last block equal {"differing": 0, "compared": null}
- PASS nodes: sampled addresses present {"differing": 0, "compared": 1910}
- PASS nodes: wallet-card counters equal {"differing": 0, "compared": null}
- PASS bal: map (address, token, kind) rows missing in V2 {"differing": 0, "compared": 28384}
- PASS bal: V2 rows not in map {"differing": 0, "compared": 28384}
- PASS bal: n_in, n_out, first/last block equal {"differing": 0, "compared": null}
- PASS bal: holds (exact sign in the map) == V2 balance > 0 {"differing": 0, "compared": null}
- PASS token_stats: n_transfers equal per token {"differing": 0, "compared": 2302263}
- PASS token_stats: tokens missing in V2 {"differing": 0, "compared": null}
- PASS counts: sum(n_in) == all token transfers {"got": "237628253", "expected": "237628253"}
- PASS counts: sum(n_out) == all token transfers {"got": "237628253", "expected": "237628253"}
- 측정: map net_approx(double) vs V2 정수 잔고 {"n": 28026, "approx_sign_differs": 6, "max_rel_err": 1.0}
- V2 bal의 erc1155 포함: True
- PASS 합계 native_tx edges.n == count(tx with to): 586294012 / 기대 586294012
- PASS 합계 erc20 edges.n == token_transfers ERC20(+WETH): 229807336 / 기대 229807336
- PASS 합계 erc721 edges.n == token_transfers ERC721: 7173998 / 기대 7173998
- PASS 합계 erc1155 edges.n == token_transfers ERC1155: 646919 / 기대 646919
- PASS 합계 sum(addresses.tx_sent) == count(tx): 587010601 / 기대 587010601
- PASS 합계 sum(addresses.tx_received) == count(tx with to): 586294012 / 기대 586294012
- PASS 합계 sum(token_transfers_out) == all token transfers: 237628253 / 기대 237628253
- PASS 합계 sum(token_transfers_in) == all token transfers: 237628253 / 기대 237628253
- PASS 합계 native edges total_amount == raw recompute (hex path): 19772125462055723586335626 / 기대 19772125462055723586335626
- PASS 합계 is_contract count == contract_creations rows (distinct): 715995 / 기대 715995
- PASS 합계 addresses unique: 11445136 / 기대 11445136
- PASS 합계 ledger sum_n_in == all token transfers: 237628253 / 기대 237628253
- PASS 합계 ledger sum_n_out == all token transfers: 237628253 / 기대 237628253
- PASS 합계 edge rows == map edge rows (map got, informational): 30278969 / 기대 None
- V2가 다시 계산하지 않는 등식: duplicate (from,to,asset,kind) keys, sum(token_stats.n_transfers) == all token transfers, sum(address_tokens.n_in) == all token transfers, sum(address_tokens.n_out) == all token transfers, native edges total_amount == addresses.native_value_received, sum(contracts_created) == contract_creations rows, sum(logs_emitted) == logs rows, unknown token standards (skipped)

### optimism

- PASS edges: keys present in map but missing in V2 {"differing": 0, "compared": 34771287}
- PASS edges: keys in V2 but not in map {"differing": 0, "compared": 34771287}
- PASS edges: n, total_amount, first/last block equal {"differing": 0, "compared": null}
- PASS nodes: sampled addresses present {"differing": 0, "compared": 1888}
- PASS nodes: wallet-card counters equal {"differing": 0, "compared": null}
- PASS 합계 native_tx edges.n == count(tx with to): 551056358 / 기대 551056358
- PASS 합계 erc20 edges.n == token_transfers ERC20(+WETH): 1423165179 / 기대 1423165179
- PASS 합계 erc721 edges.n == token_transfers ERC721: 317240027 / 기대 317240027
- PASS 합계 erc1155 edges.n == token_transfers ERC1155: 491651778 / 기대 491651778
- PASS 합계 sum(addresses.tx_sent) == count(tx): 551437487 / 기대 551437487
- PASS 합계 sum(addresses.tx_received) == count(tx with to): 551056358 / 기대 551056358
- PASS 합계 sum(token_transfers_out) == all token transfers: 2232056984 / 기대 2232056984
- PASS 합계 sum(token_transfers_in) == all token transfers: 2232056984 / 기대 2232056984
- PASS 합계 native edges total_amount == raw recompute (hex path): 6038907680670061657363146 / 기대 6038907680670061657363146
- PASS 합계 is_contract count == contract_creations rows (distinct): 379328 / 기대 379328
- PASS 합계 addresses unique: 315572412 / 기대 315572412
- PASS 합계 edge rows == map edge rows (map got, informational): 1008091223 / 기대 None
- V2가 다시 계산하지 않는 등식: duplicate (from,to,asset,kind) keys, sum(token_stats.n_transfers) == all token transfers, sum(address_tokens.n_in) == all token transfers, sum(address_tokens.n_out) == all token transfers, native edges total_amount == addresses.native_value_received, sum(contracts_created) == contract_creations rows, sum(logs_emitted) == logs rows, unknown token standards (skipped)

### robinhood

- PASS edges: keys present in map but missing in V2 {"differing": 0, "compared": 6158619}
- PASS edges: keys in V2 but not in map {"differing": 0, "compared": 6158619}
- PASS edges: n, total_amount, first/last block equal {"differing": 0, "compared": null}
- PASS nodes: sampled addresses present {"differing": 0, "compared": 1929}
- PASS nodes: wallet-card counters equal {"differing": 0, "compared": null}
- PASS bal: map (address, token, kind) rows missing in V2 {"differing": 0, "compared": 670986}
- PASS bal: V2 rows not in map {"differing": 0, "compared": 670986}
- PASS bal: n_in, n_out, first/last block equal {"differing": 0, "compared": null}
- PASS bal: holds (exact sign in the map) == V2 balance > 0 {"differing": 0, "compared": null}
- PASS token_stats: n_transfers equal per token {"differing": 0, "compared": 2350247}
- PASS token_stats: tokens missing in V2 {"differing": 0, "compared": null}
- PASS counts: sum(n_in) == all token transfers {"got": "1797632528", "expected": "1797632528"}
- PASS counts: sum(n_out) == all token transfers {"got": "1797632528", "expected": "1797632528"}
- 측정: map net_approx(double) vs V2 정수 잔고 {"n": 669796, "approx_sign_differs": 18, "max_rel_err": 1.0}
- V2 bal의 erc1155 포함: True
- PASS 합계 native_tx edges.n == count(tx with to): 961926594 / 기대 961926594
- PASS 합계 erc20 edges.n == token_transfers ERC20(+WETH): 1715558217 / 기대 1715558217
- PASS 합계 erc721 edges.n == token_transfers ERC721: 79883680 / 기대 79883680
- PASS 합계 erc1155 edges.n == token_transfers ERC1155: 2190631 / 기대 2190631
- PASS 합계 sum(addresses.tx_sent) == count(tx): 962469979 / 기대 962469979
- PASS 합계 sum(addresses.tx_received) == count(tx with to): 961926594 / 기대 961926594
- PASS 합계 sum(token_transfers_out) == all token transfers: 1797632528 / 기대 1797632528
- PASS 합계 sum(token_transfers_in) == all token transfers: 1797632528 / 기대 1797632528
- PASS 합계 native edges total_amount == raw recompute (hex path): 19306997941302951497043859 / 기대 19306997941302951497043859
- PASS 합계 is_contract count == contract_creations rows (distinct): 537944 / 기대 537944
- PASS 합계 addresses unique: 26919116 / 기대 26919116
- PASS 합계 ledger sum_n_in == all token transfers: 1797632528 / 기대 1797632528
- PASS 합계 ledger sum_n_out == all token transfers: 1797632528 / 기대 1797632528
- PASS 합계 edge rows == map edge rows (map got, informational): 365107896 / 기대 None
- V2가 다시 계산하지 않는 등식: duplicate (from,to,asset,kind) keys, sum(token_stats.n_transfers) == all token transfers, sum(address_tokens.n_in) == all token transfers, sum(address_tokens.n_out) == all token transfers, native edges total_amount == addresses.native_value_received, sum(contracts_created) == contract_creations rows, sum(logs_emitted) == logs rows, unknown token standards (skipped)

### arbitrum

- PASS edges: keys present in map but missing in V2 {"differing": 0, "compared": 1831592}
- PASS edges: keys in V2 but not in map {"differing": 0, "compared": 1831592}
- PASS edges: n, total_amount, first/last block equal {"differing": 0, "compared": null}
- PASS nodes: sampled addresses present {"differing": 0, "compared": 1907}
- PASS nodes: wallet-card counters equal {"differing": 0, "compared": null}
- PASS 합계 native_tx edges.n == count(tx with to): 2880811394 / 기대 2880811394
- PASS 합계 erc20 edges.n == token_transfers ERC20(+WETH): 3254028236 / 기대 3254028236
- PASS 합계 erc721 edges.n == token_transfers ERC721: 116359703 / 기대 116359703
- PASS 합계 erc1155 edges.n == token_transfers ERC1155: 100365903 / 기대 100365903
- PASS 합계 sum(addresses.tx_sent) == count(tx): 2881993214 / 기대 2881993214
- PASS 합계 sum(addresses.tx_received) == count(tx with to): 2880811394 / 기대 2880811394
- PASS 합계 sum(token_transfers_out) == all token transfers: 3470753842 / 기대 3470753842
- PASS 합계 sum(token_transfers_in) == all token transfers: 3470753842 / 기대 3470753842
- PASS 합계 native edges total_amount == raw recompute (hex path): 197521874385709131547780735 / 기대 197521874385709131547780735
- PASS 합계 is_contract count == contract_creations rows (distinct): 1164450 / 기대 1164450
- PASS 합계 addresses unique: 117811295 / 기대 117811295
- PASS 합계 edge rows == map edge rows (map got, informational): 727931833 / 기대 None
- V2가 다시 계산하지 않는 등식: duplicate (from,to,asset,kind) keys, sum(token_stats.n_transfers) == all token transfers, sum(address_tokens.n_in) == all token transfers, sum(address_tokens.n_out) == all token transfers, native edges total_amount == addresses.native_value_received, sum(contracts_created) == contract_creations rows, sum(logs_emitted) == logs rows, unknown token standards (skipped)

### ethereum

- PASS edges: keys present in map but missing in V2 {"differing": 0, "compared": 8267915}
- PASS edges: keys in V2 but not in map {"differing": 0, "compared": 8267915}
- PASS edges: n, total_amount, first/last block equal {"differing": 0, "compared": null}
- PASS nodes: sampled addresses present {"differing": 0, "compared": 1903}
- PASS nodes: wallet-card counters equal {"differing": 0, "compared": null}
- PASS 합계 native_tx edges.n == count(tx with to): 3787378449 / 기대 3787378449
- PASS 합계 erc20 edges.n == token_transfers ERC20(+WETH): 4069494531 / 기대 4069494531
- PASS 합계 erc721 edges.n == token_transfers ERC721: 297261769 / 기대 297261769
- PASS 합계 erc1155 edges.n == token_transfers ERC1155: 111727776 / 기대 111727776
- PASS 합계 sum(addresses.tx_sent) == count(tx): 3793898658 / 기대 3793898658
- PASS 합계 sum(addresses.tx_received) == count(tx with to): 3787378449 / 기대 3787378449
- PASS 합계 sum(token_transfers_out) == all token transfers: 4478484076 / 기대 4478484076
- PASS 합계 sum(token_transfers_in) == all token transfers: 4478484076 / 기대 4478484076
- PASS 합계 native edges total_amount == raw recompute (hex path): 11703553962930643931694563140 / 기대 11703553962930643931694563140
- PASS 합계 is_contract count == contract_creations rows (distinct): 6435103 / 기대 6435103
- PASS 합계 addresses unique: 493369976 / 기대 493369976
- PASS 합계 edge rows == map edge rows (map got, informational): 2631425746 / 기대 None
- V2가 다시 계산하지 않는 등식: duplicate (from,to,asset,kind) keys, sum(token_stats.n_transfers) == all token transfers, sum(address_tokens.n_in) == all token transfers, sum(address_tokens.n_out) == all token transfers, native edges total_amount == addresses.native_value_received, sum(contracts_created) == contract_creations rows, sum(logs_emitted) == logs rows, unknown token standards (skipped)

## 원본 이체에서 직접 다시 계산한 값과의 대조 (map 없이, 독립 경로)

- arc RAW: PASS raw rows missing in V2 0; V2 rows not in raw 0; n_in, n_out, balance (exact), first/last block equal 0; counts events_erc20 128953702; counts events_erc721 4702245; counts events_erc1155 22141
- linea RAW: PASS raw rows missing in V2 0; V2 rows not in raw 0; n_in, n_out, balance (exact), first/last block equal 0; counts events_erc1155 16413510; counts events_erc20 255740558; counts events_erc721 56604026
- ethereum LEDGER_COUNTS: PASS ledger events == all token transfers 4478484076; ledger erc20 == erc20 edges.n (ERC20+WETH) 4069494531; ledger erc721 == erc721 edges.n 297261769; ledger erc1155 == erc1155 edges.n 111727776; ledger sum_n_in == all token transfers 4478484076; ledger sum_n_out == all token transfers 4478484076; ledger unknown standards == 0 0; ledger rows after map end == 0 0; ledger input end == map end 26199999 26199999
- base LEDGER_COUNTS: PASS ledger events == all token transfers (RECHECK tt_all) 13575760513; ledger erc20 == erc20 edges.n (ERC20+WETH) 11101707102; ledger erc721 == erc721 edges.n 1567801951; ledger erc1155 == erc1155 edges.n 906251460; ledger sum_n_in == all token transfers 13575760513; ledger sum_n_out == all token transfers 13575760513; ledger unknown standards == 0 0; ledger rows after map end == 0 0; ledger input end == map end 52,789,326 52789326
- polygon LEDGER_COUNTS: PASS ledger rows == all token transfers (map checks) 26847549321; ledger rows == exact-run rows_total (independent count) 26847549321; ledger erc20 == erc20 edges.n 15317682101; ledger erc721 == erc721 edges.n 1407033135; ledger erc1155 == erc1155 edges.n 10122834085; ledger events by kind sum == rows 26847549321; ledger unknown standards == 0 0; ledger rows after map end == 0 0; ledger input end == map end 95,775,112 95775112

## Polygon focus_balances (정확값 대조)

- PASS focus_exact pairs equal in V2 (balance): 차이 0, 2^100 제외 쌍 0, 대조 21032593
- PASS V2 positive where exact is not positive (focus tokens): 차이 0, 2^100 제외 쌍 0, 대조 21032593
- PASS sign flips: V2 sign == exact sign (map double was wrong): 차이 0, 2^100 제외 쌍 0, 대조 106829
- 기준 시점: {"chain": "polygon", "risk_pairs_exact": "held", "extraction_max_block": 95775112, "econ_actions_max_block": 95775112, "rows": {"focus_exact.parquet": 21032593, "risk_pairs.parquet": 4239848, "sign_flips.parquet": 106829}, "units": "balance_raw / exact_raw / exact: HUGEINT token base units (before decimals); approx_raw / approx: DOUBLE base units (map net_approx)", "built_utc": "2026-10-20T12:07:10Z", "extraction_max_block_time_utc": "2026-10-19T00:00:15Z"}, 제외 표시 출처: none

- (아래 두 줄의 FAIL은 설명 전 원시 비교예요. 최종 판정은 '다른 쌍 설명' 줄이에요.)
- held 위험 쌍 4,239,848개 중 전수 대조 2,820,093쌍(22,263개 토큰, 남은 364개 파일에 안 나오는 토큰): FAIL, 차이 627, V2 없음 4
- 표본 토큰 297개(1,230,157쌍, 부분 합산 + 남은 파일 재계산): FAIL, 차이 22, 2^100 제외·큰 금액 관련 1
- 확인하지 못한 held 쌍: 189,598
- 다른 쌍 설명: 650쌍을 2,244개 파일 전체에서 두 번 다시 계산(모든 이체 / 2^100 이상 제외)했어요. V2 = 모든 이체 합, exact = 큰 금액 제외 합으로 650/650 설명돼요. 설명 안 되는 차이 0. 판정 PASS
- 참고: 공개된 정확값 parquet(focus_exact 등)은 HUGEINT를 DOUBLE로 저장해 2^53 넘는 값이 반올림돼 있어요. 대조는 econ.duckdb 원본 HUGEINT를 문자열로 뽑아서 했어요.

## CASHCAT 골든 케이스

- PASS creator CASHCAT n_in: 760 / 기대 760
- PASS creator CASHCAT n_out: 5 / 기대 5
- PASS creator CASHCAT first_block: 88836 / 기대 88836
- PASS creator CASHCAT last_block: 86837783 / 기대 86837783
- PASS creator CASHCAT holds (balance > 0): True / 기대 True
- PASS creator CASHCAT balance vs map net_approx (double, informational): 1511026251116619969355 / 기대 1.51102625111662e+21
- PASS CASHCAT n_transfers: 12423267 / 기대 12423267
- PASS creator out-edges rows: 123 / 기대 123
- PASS creator out-edges n_sum: 964 / 기대 964
- PASS creator out-edges cashcat_rows: 3 / 기대 3
- PASS creator node first_block: 70891 / 기대 70891
- PASS creator node last_block: 87820741 / 기대 87820741
- PASS creator node tx_sent: 762 / 기대 762
- PASS creator node tx_received: 13 / 기대 13
- PASS creator node token_transfers_in: 2828 / 기대 2828
- PASS creator node token_transfers_out: 202 / 기대 202
- PASS holders at launch (exact block 88,836) (served): 3 / 기대 3
- PASS holders at launch + 1h (exact block 90,043) (served): 90 / 기대 90

## emv2-serve 응답 vs map·원본 재계산 (기존 서비스가 없는 체인)

- arc: PASS | neighbors 주소 200 행 2795 차이 0 edges 수 차이 0 오류 0 (erc721 금액 표기 0/null 형식 차이 31) | address-chains 100 차이 0 | holders-at 토큰 12 보유자 수 차이 0 상위 차이 0
- unichain: PASS | neighbors 주소 200 행 1885 차이 0 edges 수 차이 0 오류 0 (erc721 금액 표기 0/null 형식 차이 7) | address-chains 100 차이 0

## API 대조 (emv2-serve vs 지금 서빙)


## 지연 (p50 / p99, ms)

**bench-old-skew.json** (시작 2026-10-20T11:01:21Z, load {"old": {"before": ["94.93", "49.19", "36.97"], "after": ["36.29", "36.64", "35.78"]}, "glserve_arb": {"before": ["36.29", "36.64", "35.78"], "after": ["35.63", "36.50", "35.74"]}})

| 대상 | 조회 | n | cold p50 | cold p99 | warm p50 | warm p99 | 응답 코드 |
|---|---|---|---|---|---|---|---|
| old | holders-at ARB (arbitrum) @map end | 1 | 51026.82 | 51026.82 | 5001.58 | 44770.82 | {'503': 2, '429': 1} |
| old | holders-at CASHCAT (robinhood) @map end | 1 | 62566.83 | 62566.83 | 1.14 | 30174.05 | {'503': 1, '200': 2} |
| old | trace in 3 hops (CASHCAT creator) | 1 | 770.58 | 770.58 | 0.31 | 0.56 | {'200': 3} |
| old | trace in 2 hops (arbitrum wallets) | 3 | 3782.7 | 5479.02 | 0.56 | 1.13 | {'200': 9} |
| old | address-chains (20 arbitrum wallets) | 20 | 2284.61 | 7899.7 | 0.81 | 76198.55 | {'200': 52, '429': 8} |
| glserve_arb | neighbors (20 arbitrum wallets) | 10 | 10.96 | 123.54 | 0.17 | 0.4 | {'200': 30} |

**bench-v2-vm.json** (시작 2026-10-20T14:02:14Z, load {"v2": {"before": ["22.94", "14.26", "8.85"], "after": ["24.25", "19.68", "11.67"]}})

| 대상 | 조회 | n | cold p50 | cold p99 | warm p50 | warm p99 | 응답 코드 |
|---|---|---|---|---|---|---|---|
| v2 | arc neighbors out (40 random) | 40 | 0.54 | 27.49 | 0.74 | 3.94 | {'200': 120} |
| v2 | arc neighbors out (5 hubs) | 5 | 645.26 | 695.19 | 4.17 | 8.83 | {'200': 15} |
| v2 | arc address-chains (40 random) | 40 | 0.19 | 0.36 | 0.21 | 1.06 | {'200': 120} |
| v2 | arc trace-graph in 2 hops (20 random) | 20 | 197.78 | 10809.42 | 1.31 | 3971.25 | {'200': 59, '504': 1} |
| v2 | arc trace-graph out 3 hops (10 random) | 10 | 0.41 | 1184.05 | 0.31 | 1.97 | {'200': 29, '504': 1} |
| v2 | arc path 6 (10 pairs) | 10 | 0.51 | 67301.37 | 0.53 | 134.7 | {'200': 30} |
| v2 | unichain neighbors out (40 random) | 40 | 1.01 | 8.77 | 0.28 | 1.33 | {'200': 120} |
| v2 | unichain neighbors out (5 hubs) | 5 | 39.95 | 84.48 | 2.32 | 29.43 | {'200': 15} |
| v2 | unichain address-chains (40 random) | 40 | 9.96 | 19.69 | 0.2 | 1.04 | {'200': 120} |
| v2 | unichain trace-graph in 2 hops (20 random) | 20 | 6.65 | 5300.58 | 0.52 | 3.16 | {'200': 60} |
| v2 | unichain trace-graph out 3 hops (10 random) | 10 | 0.38 | 5.51 | 0.29 | 2.79 | {'200': 30} |
| v2 | unichain path 6 (10 pairs) | 10 | 0.24 | 472.41 | 0.34 | 39.1 | {'200': 30} |

## arc 추가 확인 (전환 판정)

- accumulators AUSD 7d: PASS
- accumulators AUSD 24h: PASS
- holders-at USDC block 26,000,000: PASS
- holders-at USDC snaps=1: PASS
- arc 8af0 (WETH9 보정) 원본 재계산 대조, WETH9 제외: PASS
- WETH9(arc 8af0): V2 잔고 합 = RPC eth_getBalance(계약, 26,859,999) = 1,312,721,780,650,778,661,817, 주소별 30개 독립 재계산과 일치, 추가 이벤트 2,838,431 = Deposit 1,420,660 + Withdrawal 1,417,771 → PASS

## /api/public/em/graph (skew, arc 8af0 + unichain f3d1)

- arc: FAIL {"hop1_edges": 889, "usd_stable": 235, "nodes": 1047, "hop2_edges": 728, "nodes_labelled": 645, "hop1_nft_unkeyed": 9, "window_edges": 270, "grade_diffs": 22, "hop1_edge_diffs": 22, "window_out_of_range": 139} warm {"hops1": {"p50": 1.81, "p99": 127.3, "n": 140}, "hops2": {"p50": 1.91, "p99": 41.55, "n": 30}}
- unichain: FAIL {"hop1_edges": 575, "nodes": 741, "nodes_labelled": 280, "hop2_edges": 624, "hop1_nft_unkeyed": 5, "hop1_edge_diffs": 8, "usd_stable": 100, "window_edges": 10, "grade_diffs": 1, "usd_nonstable_wrong": 20} warm {"hops1": {"p50": 0.2, "p99": 15.31, "n": 140}, "hops2": {"p50": 0.68, "p99": 20.46, "n": 30}}
- 판정 요약: exact=true 1홉 엣지 전부 map과 같음, 2홉·노드·USD PASS. window=7d는 창 안 활동 엣지에 전체 기간 합계를 보여 줘 사양과 다름(FAIL), arc 1홉 warm p99 127ms(목표 10ms 초과).

## 판정 메모 (verify가 확인한 의미 차이)

- CASHCAT 서빙 보유자: V2 3명(블록 88,836)·90명(블록 90,043)이 원본 재계산과 같음. invscreen은 시각을 약 20초 앞 블록으로 바꾸고 먼지 보유자(12,509 wei)를 빼서 0명·87명. core 결정: V2 규칙(ts ≤ t인 마지막 블록, 잔고 > 0)이 기준, 골든 3·90으로 변경.
- linea WETH9: RPC = V2 공급량 + 0x0으로 보낸 Transfer 910건(2,414,284,440,000,590 wei) + 빠진 3행 순합 0. core가 공식을 고쳐 PASS. arc는 0x0 항 0.
- Polygon 공개 정확값 parquet는 HUGEINT를 DOUBLE로 저장(2^53 넘는 값 반올림). 대조는 econ.duckdb 원본 HUGEINT로 함.
- path(허브로): 지연은 목표 안(cold p99 174ms)이나 10쌍 중 1쌍이 예산 안에서 경로를 놓침(glserve는 3홉 발견) → 전환 보류.

## 결정성과 sanitizer

- determinism-unichain-A-vs-B-avx512.json: PASS {"build_id": "f3d124bd593053ba", "files": 59, "differ": [], "rehash_mismatch": [], "identical": true}
- determinism-unichain-A-vs-C2-avx2.json: PASS {"build_id": "f3d124bd593053ba", "files": 59, "differ": [], "rehash_mismatch": [], "identical": true}
- sanitizer-skew-1211.txt: skew integration gate (12:11 UTC, a2cb): release ctest 100% tests passed, 0 tests failed out of 19; sanitizer (-O1 -fsanitize=address,undefined -fno-sanitize-recover=undefined, detect_leaks=1) ctest 1

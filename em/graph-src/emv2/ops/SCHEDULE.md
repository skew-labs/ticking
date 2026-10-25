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

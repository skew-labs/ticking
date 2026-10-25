# skew NVMe 추가 (주인 승인 2026-10-20)

## 지금 하드웨어 (10:31 UTC, OS에서 확인)
- **섀시:** Supermicro AS-3015MR-H8TNR, 보드 H13SRD-F (AM5)
- **디스크:** Micron 7450 PRO 960GB, M.2 22110 하나예요. `M.2-C` 슬롯(PCIe 5.0 x2)에 꽂혀 있고, LVM `ubuntu-vg` 위 `/`(877G, 여유 80G, 91%)예요.
- **PCIe 슬롯 (`dmidecode -t slot`)**

  | 슬롯 | 규격 | 상태 |
  |---|---|---|
  | `RSC-S-6G5 SLOT1` (라이저) | PCIe 5.0 x8 | **비어 있음 (Available)** |
  | `MICRO-LP` | PCIe 4.0 x8 | 사용 중 (01:00.0) |
  | `M.2-C` | PCIe 5.0 x2 | 사용 중 (지금 디스크) |
- **앞쪽 핫스왑 베이:** OS에는 NVMe나 SAS 컨트롤러가 지금 디스크 말고는 보이지 않아요. 그래서 베이가 NVMe로 배선돼 있는지 OS만으로는 확인할 수 없어요. 서버 업체나 실물로 확인해야 해요.

## 추천 (주인이 살 것)
| 순위 | 디스크 | 꽂는 곳 | 비고 |
|---|---|---|---|
| **1 (확실)** | **Micron 7450 PRO 3.84TB, M.2 22110 (MTFDKBG3T8TFR)** + M.2→PCIe x4 어댑터 카드 1장 (22110 길이 지원, 방열판 포함, 라이저 높이에 맞는 브래킷) | `RSC-S-6G5 SLOT1` (비어 있음 확인) | 지금 디스크와 같은 계열이에요. 어댑터에 드라이브가 하나라 bifurcation 설정이 필요 없어요. PCIe 4.0 x4로 동작해요 |
| 2 | Micron 7450 PRO 3.84TB, U.3 2.5" (MTFDKCC3T8TFR) | 앞쪽 핫스왑 베이 | 업체가 "그 베이가 NVMe로 배선됨"을 확인해 줄 때만 써요 |

**용량:** 3.84TB를 추천해요(최소 1.92TB).
- V2 hot 전체: 약 265~290GB
- 이중 실행 기간에 기존 사본을 같이 둘 공간: 약 250GB
- 하루 delta와 다음 빌드 두 판

## 장착 뒤 우리가 할 일 (ops, root)
1. 새 장치(`/dev/nvme1n1` 예상)를 확인해요. `nvme smart-log`로 상태와 펌웨어를 봐요.
   - **지금 디스크(nvme0n1)는 절대 건드리지 않아요.** 모델과 시리얼로 두 번 확인해요.
2. GPT 파티션 하나에 `mkfs.xfs -L emv2`를 만들어요.
   - **LVM으로 루트를 넓히지 않아요.** 새 디스크가 고장 나도 루트와 운영 서비스에 영향이 없게 하려는 거예요.
3. 마운트: `/srv/emv2`, `noatime`, fstab은 UUID와 `nofail,x-systemd.device-timeout=10s`로 적어요. 디스크가 빠져도 부팅이 멈추지 않게 하려는 거예요.
4. 권한은 `ubuntu:ubuntu`로 줘요. `/srv/skew/emv2`의 체인 폴더를 옮기고, emv2-serve의 `--root`와 `ReadOnlyPaths`를 `/srv/emv2`로 바꿔요. 바꾼 뒤 SIGHUP과 health로 확인해요.
5. cgroup I/O 제한(IOWriteBandwidthMax)은 장치마다 붙어요. 새 장치에도 같은 규칙을 걸어요(EM 유닛, 차트 제외).
6. 그다음 큰 체인(ethereum → base → polygon) hot 이미지를 받아요. 첫 받기 이그레스는 $13~22예요.
7. 이중 실행이 통과하면 기존 사본 약 250GB 회수 목록을 만들어 주인 승인을 받아요(설계 9절).

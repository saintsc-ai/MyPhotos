# 프런트엔드 라이브러리 도입 계획 (Tabulator + Chart.js)

## 배경

자매 프로젝트 `pms`(Flask)가 동일한 두 라이브러리를 표준으로 쓰고 있고,
관리 페이지에서 **목록 ↔ 진척 시각화**를 둘 다 다루는 워크플로가 잘
검증되어 있어 같은 정책을 도입합니다.

- **내부망(오프라인) 친화** — CDN 미사용, 모든 파일은 `app/web/static/`
  아래 로컬에 둠
- 버전은 `scripts/download-frontend-libs.sh`에 핀 (`Tabulator 6.3.1`,
  `Chart.js 4.4.7`, `chartjs-plugin-datalabels 2.2.0`)
- 첫 셋업: `./scripts/download-frontend-libs.sh` 한 번 실행 — 약 1MB

## 정책

| 종류 | 라이브러리 | 적용 영역 |
|---|---|---|
| 표/목록 | Tabulator (`new Tabulator(...)`) | 정렬·헤더필터·컬럼토글이 필요한 모든 관리 목록 |
| 진척 시각화 | Chart.js (도넛/막대) | 비율/카운트 통계 (색인/잡큐/ML/사진종류) |
| (옵션) 트리맵 | `chartjs-chart-treemap` | 루트별/태그별 분포 — 필요 시 추가 다운로드 |
| (옵션) 엑셀 IO | SheetJS `xlsx.full.min.js` | 그리드 → 엑셀 내보내기 — 필요 시 추가 다운로드 |

## 적용 후보 — 도넛 차트 (Chart.js)

진행률 / 비율 류는 도넛이 가장 한눈에 들어옵니다. 도넛 클릭 → 해당
상태로 필터된 상세 보기로 이동하는 패턴(`pms`의 main_dashboard 참고).

| 우선순위 | 영역 | 위치 | 데이터 소스 |
|:-:|---|---|---|
| **A** | EXIF 진척 (ok / partial / failed / pending) | 관리 → 색인 | `GET /api/admin/jobs/stats` 의 `exif_*` 필드 (기존) |
| **A** | 썸네일 진척 (ok / partial / failed / pending) | 관리 → 색인 | 동일 |
| **A** | 잡 큐 (queued / running / failed / done) | 관리 → 색인 | `GET /api/admin/jobs/stats` |
| B | ML 분류 진척 (ok / failed / pending / skipped) | 관리 → 색인 (ML 섹션) | `GET /api/admin/ml/stats` |
| B | 사진 종류 분포 (image / video) | 관리 → 설정 → 데이터베이스 | `/api/admin/database/info`의 `table_row_counts` 기반 추가 집계 필요 |
| C | 루트별 사진 수 비율 | 관리 → 사진 폴더 헤더 | 신규 API 또는 클라이언트 집계 |
| C | 태그 source 분포 (user / yolo / clip / face) | 관리 → 설정 또는 새 "분석" 탭 | `GET /api/photos/tags` 응답 집계 |

> 도넛 가운데에는 총합을 큰 글씨로 표시(`datalabels` 플러그인). 색은
> 사이트 컬러 토큰 (활성=파랑, 진행중=주황, 실패=빨강, 완료=초록)에 맞춤.

## 적용 후보 — Tabulator 그리드

현재 HTML 표나 div 카드로 그려진 목록 중, **정렬·필터·컬럼 토글이
가치 있는 것**들. 시각적 카드가 더 어울리는 케이스(중복 그룹)는 변환
대상에서 제외.

| 우선순위 | 영역 | 현재 형태 | 변환 시 얻는 것 |
|:-:|---|---|---|
| **A** | 공유링크 목록 | 두 줄 카드 | 상태/소유자/만료 정렬, 토큰 검색, 일괄 revoke |
| **A** | 잡 큐 최근 실패 목록 | div 카드 | kind/status/attempts 정렬, last_error 검색 |
| B | 사진 폴더(root) 목록 | HTML `<table>` | label/path 정렬, scan_interval 토글 드롭다운 |
| B | 사용자 목록 | HTML `<table>` | 마지막 로그인 정렬, 역할 필터 |
| B | 휴지통 목록 | 무한 스크롤 + 드래그 선택 | 일괄 복구/삭제 + 정렬 + 필터 |
| 보류 | 중복 그룹 | 시각 카드 (사진 썸네일 그리드) | 카드 시각이 더 우수 — Tabulator 변환 부적합 |
| 보류 | 갤러리 사진 그리드 | 무한 스크롤 타일 | 시각 그리드가 정답 — Tabulator는 어울리지 않음 |

## 단계별 적용 (Phase)

각 phase는 별도 PR로 검증 — 한 번에 너무 많이 바꾸면 회귀 잡기 어려움.

### Phase F0 — 인프라 (이번 PR)
- `scripts/download-frontend-libs.sh`
- `docs/FRONTEND_PLAN.md` (이 문서)
- CLAUDE.md 정책 추가
- 코드 변경 없음. NAS에서 한 번 다운로드해두면 다음 phase부터 즉시 사용 가능.

### Phase F1 — 색인 탭 도넛 (Chart.js 도입 검증)
- admin.html 헤더에 `chart.umd.min.js` + `chartjs-plugin-datalabels.min.js` 로드
- `renderPhotoStats()` / `renderJobStats()` 옆에 도넛 캔버스 3개 추가
- 5초 자동 갱신 시 chart `.update()` 호출 (re-init 안 함)
- 도넛 클릭 → "최근 실패 잡" 섹션으로 스크롤 또는 `status_filter=failed` 호출

### Phase F2 — 잡/공유링크 그리드 (Tabulator 도입 검증)
- `tabulator.min.js` + CSS 로드
- 공유링크 목록을 Tabulator로 — 현재 카드 디자인은 모달 편집기로만 유지
- 잡 큐 최근 잡 목록 → Tabulator + 헤더 필터
- `column-toggle.js`를 pms에서 포팅

### Phase F3 — 폴더/사용자/휴지통 그리드
- 사진 폴더 root 표를 Tabulator로 (인플레이스 편집은 라이브러리에 맡김)
- 사용자/휴지통도 동일 패턴
- 휴지통은 무한 스크롤 vs 페이지네이션 트레이드오프 검토

### Phase F4 — ML/분포 시각화 (선택)
- ML 분류 도넛 + 사진 종류 분포 도넛
- 설정 탭 데이터베이스 카드에 시각화 추가
- 트리맵 도입(루트/태그 분포) 필요 시 `chartjs-chart-treemap` 추가 다운로드

## 의존하는 공통 유틸 (pms에서 포팅 예정)

- `static/js/column-toggle.js` — 컬럼 표시/숨기기 + localStorage 저장
- (없으면 직접 작성) 멀티셀렉트 헤더 필터 헬퍼
- 사용자 선택 에디터 (`_buildSearchableUserEditor`) — admin 사용자 관리에서 활용 가능

## 검증 절차 (각 phase 끝에)

1. NAS에서 `git pull && sudo systemctl restart myphotos-api`
2. 브라우저 강제 새로고침
3. 관리 페이지 탭 전부 한 번씩 열어보기 — 콘솔 에러 없는지
4. 모바일에서도 확인 — Tabulator는 가로 스크롤로 처리 가능하지만 도넛 폭은 미리 점검
5. DevTools Network — `tabulator.min.js` / `chart.umd.min.js`가 200 + cache 적용 확인

## 비-목표 (당분간 안 함)

- Tabulator로 갤러리 사진 그리드 대체 — 시각 타일이 답
- Tabulator로 중복 그룹 카드 대체 — 사진 시각이 답
- 외부 CDN 사용 — 내부망 정책
- React/Vue 도입 — 현재 vanilla JS로 충분, 도입은 운영 부담 증가

---
name: claude-md-management
description: Manage this project's CLAUDE.md — browse/search sections, add or edit conventions in the right section, validate & sync against real code, capture new rules from recent git changes, dedupe overlapping sections, slim for token cost, lint style, cross-check MEMORY.md, and guard against regressive edits. Use when viewing, adding to, reorganizing, checking, or trimming the CLAUDE.md project guide.
allowed-tools: Read, Edit, Write, Grep, Bash, AskUserQuestion
---

# CLAUDE.md 관리 스킬

이 프로젝트 루트의 **`CLAUDE.md`(개발 가이드)** 를 안전하게 조회·수정·검증·정리한다.
대상은 루트 `CLAUDE.md` 하나. (`MEMORY.md`, `.claude/` 자체는 편집 대상 아님 — 단 교차 점검엔 사용)

## 사용
`/claude-md-management [동작] [내용...]` — 인자로 동작 지정. 인자 없으면 **섹션 지도**를 보여주고 무엇을 할지 묻는다.

| 동작 | 성격 | 의미 |
|---|---|---|
| `list` (인자 없음) | 읽기 | 섹션 지도 출력 |
| `show <섹션>` | 읽기 | 특정 섹션 원문 |
| `search <키워드>` | 읽기 | 키워드를 다루는 섹션 위치(추가 전 중복 방지) |
| `add <규칙>` | 편집 | 올바른 섹션에 새 규칙 추가 |
| `edit <섹션>` | 편집 | 해당 섹션만 수정 |
| `toc` | 편집 | 상단 목차 생성/갱신 |
| `dedupe` | 편집 | 겹치는/중복 섹션 통합 제안·병합 |
| `slim` | 편집 | 분량/토큰 관리(요약·분리) |
| `validate` | 점검 | 구조 검증(링크·구식참조·펜스) |
| `sync` | 점검 | 문서↔실제 코드 드리프트 대조 |
| `lint` | 점검 | 스타일/포맷 규칙 점검 |
| `capture` | 점검→편집 | 최근 git 변경에서 새 규칙 추출·반영 제안 |
| `guard` | 회귀 | 편집 전/후 회귀 점검(섹션 유실·링크·펜스) |

## 공통 원칙 (모든 동작)
- **수정 전 반드시 `Read`** 로 현재 내용 확인. 편집은 `Edit`(부분 치환) **최소 변경** — 전체 재작성 금지.
- 문서 언어 **한국어**, 기존 톤 유지.
- 새 규칙은 **가장 관련 있는 기존 섹션**에 넣는다(아래 배치 가이드). 애매하면 `AskUserQuestion`으로 확인. 없으면 적절한 `##` 아래 새 `###` 생성.
- **근거 우선**: 규칙에 파일·함수·플래그·컬럼을 쓰면 `Grep`으로 존재를 확인하고, 없는 건 쓰지 않는다.
- **범위 잠금**: 이 스킬은 **CLAUDE.md만** 수정한다. 코드 변경·배포·커밋은 하지 않는다(필요하면 사용자에게 별도 요청 안내).
- **미리보기→적용(propose→apply)**: 한 섹션 초과의 큰 변경은 **먼저 변경안(diff 요약)을 제시하고 승인 후** 적용. 승인 뒤 `git diff CLAUDE.md`로 결과 요약 가능.
- **편집 전 자동 백업**: 대규모 재구성(dedupe/slim/toc/여러 섹션) 전 `cp CLAUDE.md /tmp/CLAUDE.md.bak-<시각>` 로 백업(되돌리기용). 마치면 백업 경로 안내.
- **회귀 점검 필수**: 모든 편집 동작(add/edit/toc/dedupe/slim/capture) **직후 `guard`를 실행**해 회귀가 없는지 확인하고 결과를 보고한다.
- 편집 후 **무엇을 어느 섹션에 바꿨는지** 한국어로 요약 보고.

## 섹션 지도 (최상위 `##` / 주요 `###`)
- **프로젝트 개요 / 프로젝트 구조**
- **코딩 컨벤션**: 데이터베이스 · FastAPI 라우터 패턴 · 권한 제어 · API 응답 형식 · 프론트엔드(vanilla JS)
- **워커 & 작업 큐**: 인덱싱 vs ML 워커 · photo_work/jobs 큐 · ML 실행 프로바이더
- **주요 테이블**: photos · photo_work · photo_locations(source) · roots · 기타
- **자주 쓰는 패턴**: 확인/알림 다이얼로그 · i18n · 커스텀 날짜 피커 · 서비스워커 캐시 · 테마
- **실행 방법 / 환경 변수**
- **중요 규칙**: DB 마이그레이션(워커 정지) · 배포(Synology git pull) · 커밋 규칙 · 정적 파일 변경 후 검증
- **주요 기능별 파일**: 검색/FTS · 위치/지도 · ML 파이프라인 · 얼굴 검색 · 중복 제거 · 운영 CLI

> 배치 가이드: 프론트/UI 규칙(다이얼로그·i18n·피커·테마·서비스워커) → **자주 쓰는 패턴**, 서버/DB/라우터/권한 컨벤션 → **코딩 컨벤션**, 워커·큐·ONNX → **워커 & 작업 큐**, 배포·마이그레이션·커밋·검증 → **중요 규칙**, 기능 위치 안내 → **주요 기능별 파일**.

## 동작별 절차

### list / (인자 없음)
`grep -nE "^#{1,3} " CLAUDE.md` 로 헤딩을 뽑되 **코드펜스(```) 안의 `#` 주석은 섹션이 아니므로 제외**(예: `# 개발: API`, `# 워커`). 위 섹션 지도 형태로 보여주고 다음 동작을 묻는다.

### show <섹션> / search <키워드>
- show: 해당 헤딩부터 다음 동급 이상 헤딩 전까지 `Read`(offset/limit)로 그대로 보여준다.
- search: `Grep -n <키워드> CLAUDE.md` → 매칭 라인이 속한 섹션(가장 가까운 상위 헤딩)을 알려준다.

### add <규칙>
1. 규칙 성격 판단 → 배치 섹션 결정(배치 가이드). 애매하면 질문.
2. 근거 파일·함수·컬럼 `Grep` 확인.
3. 기존 하위 섹션 스타일(제목 `### 제목 ([파일](경로))`, 코드블록, 클릭 가능한 `file:line` 링크)에 맞춰 작성.
4. `Edit`로 해당 `##` 섹션 적절 위치에 삽입 → `guard`.

### edit <섹션>
해당 섹션만 `Read` → 최소 `Edit` → `guard`. 다른 섹션 불변.

### toc
현재 상단 목차 없음. 요청 시 `##` 기준 목차를 제목 다음에 생성/갱신. 앵커는 GitHub 슬러그(소문자·공백→`-`·특수문자 제거). → `guard`.

### dedupe — 중복/겹치는 섹션 통합
1. 유사 주제 섹션을 찾는다(예: 큐/우선순위 규칙이 **워커** 섹션과 **패턴**에 흩어지거나, 위치/`source` 규칙이 여러 곳에).
2. 겹치는 규칙을 표로 정리해 **통합안 제시**(어느 섹션으로 합칠지, 무엇을 남길지).
3. 사용자 승인 후 백업→병합→교차링크 갱신→`guard`.

### slim — 분량/토큰 관리
CLAUDE.md는 **매 세션 컨텍스트에 로드**됨.
1. 섹션별 라인수 리포트(`awk`로 헤딩 사이 라인수 집계) + 상위 N개 표시.
2. 장황한 코드 예시는 **핵심만 남기고 요약**하거나, 긴 설명은 `docs/`(또는 참조 파일 경로 링크)로 분리 제안.
3. 승인 후 백업→편집→`guard`. 총 라인수 감소분 보고.

### validate — 구조 검증(자동 수정은 승인 후)
1. **중복/모호 헤딩**: 동명 `##`/`###` 중복.
2. **깨진 내부 링크**: `[..](path)` 경로 존재(`test -e`), 라인앵커(`#L..`) 파일 존재.
3. **코드펜스 균형**: ```` ``` ```` 개수가 짝수인지.
4. **구식 참조**: 없어진 파일/함수/컬럼을 규칙이 가리키지 않는지(→ `sync`와 연계).
5. **의존성 표기**: `pyproject.toml`/`requirements.txt`에 없는 라이브러리를 전제하지 않는지(참고).
> 오프라인 아님: 이 프로젝트는 일부 외부 CDN(예: index.html의 unpkg leaflet)을 실제로 쓴다 → "외부 CDN 금지"를 규칙으로 넣지 말 것.

### sync — 문서↔코드 드리프트 대조
규칙이 가리키는 **파일·함수·플래그·컬럼·엔드포인트가 실제로 존재하고 문서 설명과 일치**하는지 대조. 변경이 잦아 문서가 뒤처지기 쉬운 지점:
- 워커: `STAGE_ORDER`·`PRIO_*` 상수·`enqueue_stage` 시그니처(`app/worker/photo_work.py`).
- ML: `_ort.make_session`의 provider 목록/`MYPHOTOS_ONNX_PROVIDERS`(`app/worker_ml/_ort.py`).
- 위치: `photo_locations.source` 허용값(`exif/estimated/user/NULL`)(`app/models.py`, `worker/location_estimator.py`).
- 프론트: `js/panels/*.js` 파일명, `window.uiConfirm/_t` 등 전역, `sw.js`의 `VERSION`, `attachCustomDatePicker`(index.html).
- 상태 컬럼: `photos.*_status` 이름(`app/models.py`).
1. 규칙에서 언급된 이름을 뽑아 `Grep`으로 존재 확인 → 없으면 **"구식 참조"** 목록.
2. 동작 설명과 실제 코드가 어긋나는 지점 지적.
3. 갱신안 제시 → 승인 후 편집 → `guard`.

### lint — 스타일/포맷
- 섹션 제목 형식, 코드펜스에 language 태그, 파일 참조는 **클릭 가능한 `[텍스트](경로#Ln)`**(백틱 경로 지양), 표/불릿 포맷 일관성, 한국어 톤. 위반 목록 + 제안.

### capture — 최근 git 변경에서 규칙 추출
1. `git log --oneline -n <N>` / `git diff <ref> HEAD --stat` 로 최근 변경 파악(N 미지정 시 최근 20 또는 마지막 CLAUDE.md 수정 이후).
2. 새로 생긴 **공용 유틸(`js/*.js`)·워커 단계·엔드포인트·설정 플래그·마이그레이션**을 추려 CLAUDE.md 반영 후보를 제시(예: 새 provider 모드, 새 `app/tools/*` CLI, 새 status 컬럼 등).
3. 승인 항목만 `add` 절차로 반영 → `guard`.

### 메모리 교차 점검 (validate/lint에 포함)
- 오토메모리(`~/.claude/projects/<이 프로젝트 슬러그>/memory/MEMORY.md` 및 개별 메모리)와 CLAUDE.md 규칙이 **충돌/중복**되는지 점검. 같은 내용이 양쪽에 있으면 어디를 정본으로 할지 제안(코드로 검증 가능한 규칙은 CLAUDE.md, 비공개/일회성 맥락은 메모리).

### guard — 회귀 점검(문서 회귀 방지)
편집으로 **의도치 않은 손상(회귀)** 이 없는지 확인. 편집 동작 후 **항상 실행**하고, 단독 호출도 가능.
1. **편집 전 스냅샷**(가능하면): 라인수, `##`/`###` 헤딩 목록, 링크 목록, 코드펜스 개수, 파일 크기.
2. **편집 후 비교**로 아래 회귀 검출:
   - **섹션 유실**: 의도치 않게 사라진 헤딩(추가/이동이 아닌 삭제).
   - **링크 깨짐**: 새로 깨진 `[..](path)`.
   - **코드펜스 불균형**: ```` ``` ```` 홀수(렌더 깨짐).
   - **급격한 분량 변화**: 예상치 못한 대량 삭제(오편집 신호).
3. 이상 발견 시 **즉시 알리고, `/tmp` 백업으로 되돌리기**를 제안. 정상이면 "회귀 없음" 보고.

## 이 프로젝트의 "변경 후 검증(회귀) 규칙" 지원
CLAUDE.md `## 중요 규칙`에 검증 절차가 일부 있다. 실제 이 저장소가 매번 수행하는 것 → `add`/`capture`로 명문화하도록 돕는다:
- **프론트(인라인 JS/CSS) 수정 후**: 구문/균형 확인, 가능하면 **헤들리스 Chrome 하니스**(`--headless=new --screenshot`)로 렌더 시각 검증(로컬 인증 없이 컴포넌트 확인).
- **정적 셸 변경 시 `sw.js`의 `VERSION` 올림** + 강력 새로고침(서비스워커 캐시).
- **DB 스키마 변경(alembic)** 은 **워커 정지 후** 실행(SQLite 락). 손검증은 라운드트립/원복.
- **서버는 자동 리로드 없음**(운영) → `systemctl restart` 후 확인. 개발은 `uvicorn --reload`.
- **i18n 문자열**은 카탈로그에 없으면 `_t(key, fallback)` 폴백으로 동작 → 필요 시 10개 언어 카탈로그 동기화.

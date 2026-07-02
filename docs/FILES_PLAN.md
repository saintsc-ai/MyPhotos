# 일반 파일관리(문서) 설계 노트

MyPhotos에 **가벼운 보관·공유 + 빠른 검색**을 목표로 일반 파일(문서) 관리를 추가한다.
결정 요지: **별도 프로젝트로 포크하지 않고 같은 저장소·같은 서버에서 확장**하되,
`photos`에 흡수하지 않고 **형제 `files` 도메인**으로 둔다. 인프라(auth/ACL/roots/scan/
jobs/shares/FTS/upload/desktop/배포)는 그대로 재사용한다.

## 1. 저장 — 루트(폴더) 속성으로 분기
- NAS에서 사진 폴더와 문서 폴더가 물리적으로 분리돼 있으므로, 파일 단위 mime 분류가 아니라
  **루트에 타입 속성**을 둔다: `roots.kind = 'photo' | 'file'`(기본 `'photo'`, 하위호환).
- 관리자 "루트 추가" UI에 타입 선택(사진 / 문서) 추가.
- `scanner/discover.py`는 이미 루트 단위 실행 → 진입부에서 `root.kind`로 분기:
  - `photo` → 현행 그대로(미디어만 `Photo`, 비미디어 스킵). **변경 없음.**
  - `file` → 비-무시 파일을 `files` 행으로 색인 + 가벼운 잡 enqueue.
- 사진 파이프라인(EXIF/썸네일/ML)은 photo-root에만 걸리므로 문서엔 자동으로 안 붙는다.

## 2. 데이터 모델 (신규, `photos` 불변)
```
files(id, root_id→roots, rel_path, sha256, mime, size, mtime,
      status(active/missing/trashed),
      text_status(pending/ok/none/failed), text_engine,
      owner_user_id→users, created_at)
UNIQUE(root_id, rel_path)          # 사진과 동일: POSIX·NFC 상대경로
files_fts                          # photos_fts 미러: rowid=file_id, text=파일명+경로+추출텍스트
share_file_items(share_id→shares, file_id→files, sort_idx)   # 공유 일반화(비파괴)
roots.kind                         # 'photo' | 'file'
```
- 마이그레이션 1개(0038)에 묶고 **워커 정지 후** 적용(SQLite 락 규칙).
- 공유는 기존 `shares`를 재사용하고 항목만 `share_file_items`로 추가 → 사진 공유 무영향.

## 3. 색인 — 빠른 경로 + 내용 추출(단계 분리)
- **빠른 경로(즉시)**: 해시 + mime + size/mtime → **파일명·경로로 즉시 검색 가능**.
  (파일 도메인의 강점 = 즉시성. 사진의 단계별 status 사상과 동일.)
- **내용 추출(뒤따라 채움)**: 확장자→추출기 **레지스트리**, 각 티어 **optional**(미설치 시
  `text_status='none'`, 색인은 절대 실패하지 않음). 추출 텍스트는 앞 N KB만 저장(검색 적합성
  충분·저장 폭주 방지), `text_engine`/버전 기록으로 나중에 재처리 가능.

### 내용검색 포맷 티어
| 티어 | 포맷 | 추출 | 무게 |
|---|---|---|---|
| 0 (항상) | txt·md·csv·log·json·xml·html·소스 | 표준 디코드(**utf-8→cp949/euc-kr 폴백**) | 없음 |
| 1 (권장 기본) | DOCX·XLSX·PPTX·RTF·**텍스트 PDF** | python-docx·openpyxl·python-pptx·striprtf·pypdf(또는 PyMuPDF) | 경량(순수 파이썬) |
| 2 (한국어) | **HWP·HWPX** | **rhwp 파이썬 바인딩**(golbin/hop의 엔진, PyO3) | 선택 의존성 |
| 3 (폴백·opt-in) | 스캔 PDF·이미지 문서 / 레거시 OLE(.doc/.xls) | **RapidOCR 재사용**(네이티브 추출이 비면 폴백) / LibreOffice headless | 무거움 |

- **OCR은 만능 리더가 아님**: 디지털 문서(PDF-텍스트·DOCX·HWP)는 내부 텍스트를 추출(티어1/2),
  OCR(티어3)은 **스캔/이미지 문서 폴백**으로만.
- **PDF 라이브러리**: PyMuPDF가 가장 빠르고 미리보기 렌더까지 되나 **AGPL** → 라이선스가 걸리면
  pypdf(MIT)로 텍스트만.
- **rhwp**: HWP/HWPX→마크다운/평문/JSON, 스트리밍 API + Python 바인딩. optional 의존성으로 두고
  미설치 시 HWP는 이름검색만. (패키지명·라이선스는 도입 시 확정)

## 4. 탐색 UI — 헤더 모드 스위치
- 헤더에 세그먼트 컨트롤 **`[📷 사진 | 📁 파일]`**. 모바일은 아이콘만.
- 로그인 시 **마지막 모드 기억**(`localStorage("myphotos-mode")`, 기본 사진).
- **권한 기반 자동 라우팅**: 접근 가능한 루트 `kind`가 한 종류뿐이면 그쪽으로 바로 + **스위치 숨김**.
- 모드 = **탭 세트 교체**:
  - 사진: 타임라인·지도·폴더·주제(기존).
  - 파일: 폴더·목록·검색(신규, 단순).
- 검색창은 공유하되 현재 모드로 스코프(사진 FTS / 파일 FTS 분리).
- 새 정적 파일 → `sw.js` `VERSION` 올림.

## 5. API / 프론트
- `app/api/routes_files.py`(신설, `auth_only`): 목록/폴더 탐색·메타·**다운로드(원본 스트리밍)**·
  검색·업로드·삭제(휴지통). 접근권한은 `require_*_level`/root_acl/folder_acl 재사용.
- 사진 API는 `kind='photo'`, 파일 API는 `kind='file'` 루트만 대상(회귀 방지).
- `js/panels/files.js`(신 IIFE): 폴더 탐색+목록+검색+다운로드+공유+업로드. `uiConfirm/_t`·날짜필터 재사용.
  라이트박스/지도/ML 없음. 일반 아이콘 + (선택) PDF/이미지 미리보기.

## 6. 재사용 vs 신규
| 신규(얇게) | 그대로 재사용 |
|---|---|
| `roots.kind`·`files`·`files_fts`·`share_file_items`, 마이그레이션 0038 | roots/스캔, ACL, jobs/디스패처, shares, 업로드, 다운로드 스트리밍, 감사, i18n, SW, 데스크톱, 배포, cutover |
| `routes_files.py`, `panels/files.js`, "파일" 탭 + 헤더 스위치 | 탭/뷰 토글, uiConfirm/_t, 날짜피커 |
| 추출기 레지스트리(티어 0/1/2/3) | RapidOCR 엔진(티어3), external 도구 패턴(app/external.py) |

## 7. 단계별 진행 + 검증
1. **Phase 1** 모델(`roots.kind`+`files`+`files_fts`+`share_file_items`) + alembic 0038.
   검증: 로컬 라운드트립, 서버는 워커정지 규칙.
2. **Phase 2** 스캐너 `root.kind` 분기 + 빠른 색인 잡. 검증: 문서 루트가 `files`에 잡히는지.
3. **Phase 4–5** `routes_files.py` + "파일" 탭 + 헤더 스위치. 검증: 헤들리스 렌더, 다운로드.
4. **Phase 3** 내용 추출 티어 0/1 → FTS. 이후 티어 2(rhwp)·3(OCR) 선택 추가.
5. **Phase 6** 공유(share_file_items)·업로드·공개 공유 페이지 파일 렌더.

## 8. 파일 탐색기 UI/UX (파일 모드)
윈도우 익스플로러 형태의 GUI 폴더 창:
- **좌: 폴더 트리** / **우: 목록(이름·크기·수정일·종류, 정렬) 또는 아이콘 보기**.
- 상단: **브레드크럼 + 툴바**(선택 기반), 헤더: **검색창**(파일 스코프).
- 상호작용: 폴더 더블클릭 진입, 다중선택(Ctrl/Shift), **우클릭 컨텍스트 메뉴**, 드래그&드롭 업로드, 상태 바(선택 수·용량).
- **작업 범위 = 풀 익스플로러**(업로드·새폴더·이름변경·이동·삭제). 단 **쓰기 가능 루트에서만**(`roots.readonly`로 자동 표시/숨김). 읽기전용 문서 루트는 보기·검색·다운로드·공유만.
- API는 **읽기(탐색/다운로드/검색) → 쓰기(편집)** 순서로 구현.
  - 읽기(구현·검증됨): `GET /api/files/roots|list|search|{id}|{id}/download`([routes_files.py](../app/api/routes_files.py)). ACL은 폴더 단위(`effective_folder_level`) 재사용.
  - 쓰기(예정): 업로드·mkdir·rename·move·delete — 파일시스템 변경 + DB 재조정.

## 참고(범위 밖)
- **미리보기**: rhwp/‌PyMuPDF로 HWP·PDF 첫 페이지 렌더 → 공유 강점 보강(후속).
- **편집**: golbin/hop(HWP 에디터, MIT)이 레퍼런스. 데스크톱 에디터 통합은 대형 범위라 v1 제외.

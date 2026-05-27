# 접근 권한 (Access Control) 도입 계획

> 가족용 자가호스팅 사진 카탈로그에 다단계 권한을 도입한다. 한 번
> 도입하면 마이그레이션·UX·머슬메모리 측면에서 되돌리기 어려우므로,
> 모든 phase의 스키마·라우터 영향·UI 변경·엣지케이스를 사전에 확정한다.
>
> 작성: 2026-05-27, 사용자 합의: 5단계 ACL / liberal default / P1→P5 순차 진행.

---

## 1. 목표 / 비목표

### 목표
- **부모/자녀 분리** — 부모만 보이는 폴더, 사진, 단일 이미지를 명시적으로 만들 수 있어야 한다.
- **사고 방지** — 어린 가족 구성원이 다른 사람 사진을 실수로 삭제/편집 못 하게.
- **외부 공유 통제** — 공유링크 생성을 일부 사용자만 가능하게.
- **감사** — 누가 언제 무엇을 지웠는지 추적.
- **하위호환** — 기존 사용자는 P1 적용 직후에도 기능 손실 없음 (liberal default).

### 비목표
- 엔터프라이즈 RBAC (그룹, 역할 상속, 권한 위임 등). 평면 ACL로 충분.
- 외부 공유링크 수신자(viewer)에 대한 세분화 — 공유링크 자체가 권한.
- 본인 평점/댓글/본인 태그는 항상 가능 (기본 사용자 액션, 권한 분리 X).

---

## 2. 권한 모델

### 2.1 사용자 글로벌 플래그 (P1)

`users` 테이블에 boolean 4개 추가. admin은 모든 플래그를 무시(우회)한다.

| 플래그 | 신규 기본 | 기존 사용자 마이그레이션 | 의미 |
|---|---|---|---|
| `can_upload` | `false` | `true` | 새 사진 업로드 (관리 UI / 폴더 업로드 / API) |
| `can_delete` | `false` | `true` | 휴지통 보내기 + 영구 삭제 |
| `can_share` | `false` | `true` | 외부 공유링크 생성 |
| `can_edit_meta_others` | `false` | `true` | **본인이 만들지 않은** 사진의 태그/캡션/날짜 편집 |
| `is_admin` (기존) | `false` | (기존값 유지) | root 관리, 사용자 관리, ACL 관리, 모든 ACL 우회 |

> `can_view`, `can_edit_meta_own`, `can_rate`, `can_comment`는 항상 true.
> 본인 평점/댓글/본인이 단 태그는 권한과 무관.

### 2.2 리소스 ACL 레벨 (P2/P3/P4 공통)

ACL 엔트리는 `level` 컬럼에 5단계 enum 중 하나를 가진다 (위로 갈수록 권한 추가):

| 레벨 | 가능 동작 (글로벌 플래그가 허용한다는 전제) |
|---|---|
| `hidden` | 보이지 않음 (모든 응답이 마치 그 사진이 없는 것처럼) |
| `read` | 보기, 다운로드, 메타 조회 |
| `interact` | read + **본인** 평점·댓글·본인 태그 |
| `contribute` | interact + 태그·캡션·날짜 등 메타 편집 (`can_edit_meta_others`도 필요) |
| `manage` | contribute + 휴지통 보내기 (`can_delete`도 필요) + 폴더 생성/이름변경 |

> 폴더 삭제, root 추가/삭제, 사용자 관리 등 "root 수준 운영"은 ACL 레벨이 아니라 `is_admin` 전용. ACL을 잘못 줘서 권한 escalation이 생기지 않게.

### 2.3 ACL 적용 우선순위 (가장 구체적인 게 이김)

한 사진에 대해 effective level은 다음 순서로 결정:

```
1. photos.visibility == 'private'  → owner + admin만, 그 외 hidden
2. photos.visibility == 'public'   → 강제 read 이상 (hidden 우회)
3. folder_acl 매칭 중 가장 긴 path_prefix 엔트리
4. root_acl 엔트리
5. 디폴트 = read
```

`hidden`은 한 번이라도 매칭되면 즉시 컷오프. 단, 더 구체적인(=긴 prefix) ACL이 `read` 이상으로 명시되어 있으면 그 폴더는 보임 (사용자 의도). 예:

- `root_acl(/photo, user_kid) = hidden` + `folder_acl(/photo/kid_own, user_kid) = manage` →
  - `/photo/family_album/foo.jpg` → hidden
  - `/photo/kid_own/bar.jpg` → manage

### 2.4 유효 권한 계산

```python
def effective_level(user, photo) -> str:
    if user.is_admin:
        return "manage"                    # admin은 모든 ACL 우회
    if photo.visibility == "private":
        return "manage" if photo.owner_user_id == user.id else "hidden"
    # photo.visibility == "public" → 디폴트가 read로 고정, hidden ACL 무시
    base = "read" if photo.visibility == "public" else None

    # folder_acl (가장 긴 path_prefix)
    fa = match_folder_acl(user.id, photo.root_id, photo.rel_path)
    if fa: base = fa.level
    # root_acl (folder_acl 없을 때만)
    elif base is None:
        ra = match_root_acl(user.id, photo.root_id)
        base = ra.level if ra else "read"

    return base
```

쿼리 시점 필터는 `level != 'hidden'`만 SQL로 처리; 세분화 동작 권한은 mutating 엔드포인트에서 effective_level을 다시 계산해 확인.

---

## 3. 데이터 모델 (alembic revs)

### 3.1 P1 — `users` 확장 (alembic 0012)

```sql
ALTER TABLE users ADD COLUMN can_upload BOOLEAN NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN can_delete BOOLEAN NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN can_share BOOLEAN NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN can_edit_meta_others BOOLEAN NOT NULL DEFAULT 0;

-- Liberal default: 기존 사용자는 모두 true (admin 포함, viewer 포함).
UPDATE users SET
  can_upload = 1, can_delete = 1, can_share = 1, can_edit_meta_others = 1;
```

### 3.2 P2 — `root_acl` (alembic 0013)

```sql
CREATE TABLE root_acl (
    root_id   INTEGER NOT NULL REFERENCES roots(id) ON DELETE CASCADE,
    user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    level     TEXT    NOT NULL CHECK (level IN ('hidden','read','interact','contribute','manage')),
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (root_id, user_id)
);
CREATE INDEX ix_root_acl_user ON root_acl(user_id);
```

엔트리 없음 = 디폴트(`read`). 명시적 `hidden` 행이 있어야 가린다.

### 3.3 P3 — `folder_acl` (alembic 0014)

```sql
CREATE TABLE folder_acl (
    root_id      INTEGER NOT NULL REFERENCES roots(id) ON DELETE CASCADE,
    path_prefix  TEXT    NOT NULL,                  -- 'family/private/' (POSIX, trailing slash 필수)
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    level        TEXT    NOT NULL CHECK (level IN ('hidden','read','interact','contribute','manage')),
    created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (root_id, path_prefix, user_id)
);
CREATE INDEX ix_folder_acl_user_root ON folder_acl(user_id, root_id);
```

매칭 알고리즘: `WHERE :rel_path LIKE folder_acl.path_prefix || '%'` 중 `LENGTH(path_prefix) DESC LIMIT 1`. SQLite는 prefix index 못 쓰지만 한 사용자의 폴더 ACL은 보통 수십 건 이하라 OK.

### 3.4 P4 — 사진 단위 private (alembic 0015)

```sql
ALTER TABLE photos ADD COLUMN owner_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE photos ADD COLUMN visibility TEXT NOT NULL DEFAULT 'inherit'
    CHECK (visibility IN ('inherit','private','public'));

CREATE INDEX ix_photos_owner ON photos(owner_user_id) WHERE owner_user_id IS NOT NULL;
```

`owner_user_id`는 P1 이후 `photo_uploads` 트래킹이 있을 때 자동 채움. 마이그레이션 시점엔 모두 NULL (기존 사진은 주인 없음 = admin만 private 토글 가능).

### 3.5 P5 — 휴지통 격리 + 감사 로그 (alembic 0016)

```sql
ALTER TABLE photos ADD COLUMN trashed_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL;

CREATE TABLE audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    user_id      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    username     TEXT,                            -- 비정규화 (사용자 삭제 후에도 누가 했는지 보존)
    action       TEXT NOT NULL,                   -- 'photo.trash' | 'photo.purge' | 'share.create' | 'acl.change' | ...
    resource_type TEXT NOT NULL,                  -- 'photo' | 'root' | 'folder' | 'share' | 'user' | 'acl'
    resource_id  TEXT,                            -- int 또는 'root_id:path' 등 자유형식
    detail       TEXT                             -- JSON: 변경 전/후, 추가 컨텍스트
);
CREATE INDEX ix_audit_ts ON audit_log(ts DESC);
CREATE INDEX ix_audit_user ON audit_log(user_id, ts DESC);
CREATE INDEX ix_audit_resource ON audit_log(resource_type, resource_id, ts DESC);
```

---

## 4. 백엔드 영향 매트릭스

### 4.1 공통 헬퍼 (P2부터 등장)

`app/auth_acl.py` 신설:

```python
def visible_photo_ids_subquery(db, user) -> Select:
    """Select Photo.id of every photo this user can see (level != hidden)."""

def effective_level(db, user, photo) -> Literal['hidden','read','interact','contribute','manage']:
    """Compute effective level for a single photo."""

def require_level(user, photo, min_level) -> None:
    """Raise 403 if effective_level(user, photo) < min_level."""

def require_flag(user, flag_name) -> None:
    """Raise 403 if user.<flag_name> is False (and user not admin)."""
```

### 4.2 영향 받는 엔드포인트

각 엔드포인트가 어떤 phase에서 무엇이 바뀌는지:

| 엔드포인트 | P1 | P2 | P3 | P4 | P5 |
|---|---|---|---|---|---|
| `GET /api/photos` (목록) | — | ACL 필터 | + folder ACL | + visibility | — |
| `GET /api/photos/{id}` | — | hidden→404 | hidden→404 | private→404 | — |
| `GET /api/photos/{id}/thumb`, `/full` | — | hidden→404 | hidden→404 | private→404 | — |
| `GET /api/photos/locations*` | — | ACL 필터 | + folder | + visibility | — |
| `GET /api/photos/in-cell` | — | ACL 필터 | + folder | + visibility | — |
| `GET /api/photos/tags` | — | ACL 필터 | + folder | + visibility | — |
| `GET /api/photos/folders` | — | ACL 필터 | + folder | — | — |
| `GET /api/photos/roots` | — | hidden root 제외 | — | — | — |
| `POST /api/photos/{id}/comments` | — | require interact | + folder | + visibility | — |
| `PATCH /api/photos/{id}/rating` | — | require interact | + folder | + visibility | — |
| `PATCH /api/photos/{id}` (메타 수정) | `can_edit_meta_others` | require contribute | + folder | + visibility | — |
| `POST /api/photos/{id}/tags` (본인 태그) | — | require interact | + folder | + visibility | — |
| `DELETE /api/photos/{id}/tags/{tagid}` (남의 것) | `can_edit_meta_others` | require contribute | + folder | + visibility | — |
| `DELETE /api/photos/{id}` (휴지통) | `can_delete` | require manage | + folder | private 시 owner만 | `trashed_by_user_id` 기록 |
| `POST /api/photos/{id}/restore` (휴지통→복구) | `can_delete` | require manage | + folder | — | audit 기록 |
| `POST /api/photos/bulk-delete` | `can_delete` | per-photo manage | per-photo | per-photo | per-photo trashed_by |
| `GET /api/admin/trash` | — | — | — | — | **본인 것만 (admin은 전체)** |
| `POST /api/shares` (공유 생성) | `can_share` | per-photo read | per-photo | per-photo | audit |
| `DELETE /api/shares/{id}` | `can_share` (또는 owner) | — | — | — | audit |
| `POST /api/admin/folders` (폴더 생성) | `can_upload` | root manage 필요 | folder manage | — | audit |
| `POST /api/admin/folders/upload` (업로드) | `can_upload` | folder manage | folder | — | audit |
| `DELETE /api/admin/folders` (폴더 삭제) | `can_delete` | folder manage | folder | — | audit + per-photo trashed_by |
| `PATCH /api/admin/folders/rename` | `can_edit_meta_others` | folder manage | folder | — | audit |
| `PATCH /api/admin/users/{id}` (권한 변경) | admin only | — | — | — | audit (acl.change) |
| `POST /api/admin/root_acl` (P2 신규) | — | admin only | — | — | audit |
| `POST /api/admin/folder_acl` (P3 신규) | — | — | admin only | — | audit |

### 4.3 공유링크 처리

`share_items.photo_id`로 묶인 외부 공유는 **링크 자체가 권한**이므로 viewer ACL을 우회한다. 하지만:

- **생성 시점**: 공유 생성자가 그 photo_id들에 대해 `read` 이상이어야 함. 아니면 403.
- **viewer 입장**: 토큰만 있으면 사진은 보인다. P4 `private` 사진을 누가 공유했어도 viewer는 본다 (이미 공유한 시점에 동의한 셈).
- P5에서: 공유 생성·revoke 모두 audit 기록.

---

## 5. UI 영향

### 5.1 P1 — 관리 → 사용자

기존 사용자 행에 권한 체크박스 4개 추가:

```
사용자명 | 권한 | 업로드 | 삭제 | 공유 | 메타편집 | 최종 로그인 | 작업
admin   | 관리자 | ✓     | ✓   | ✓    | ✓        | ...        | 비번 변경 / 삭제
mom     | 일반  | ✓     | ✓   | ✓    | ✓        | ...        | ...
kid     | 일반  | ✗     | ✗   | ✗    | ✗        | ...        | ...
```

각 체크박스 변경 시 `PATCH /api/admin/users/{id}` 즉시. admin은 체크박스 비활성(항상 모든 권한).

### 5.2 P2 — 관리 → 사진 폴더

각 root 행 끝에 **"권한"** 버튼 추가 → 모달:

```
┌─ family root 권한 설정 ────────────────┐
│ 기본 = read (엔트리 없는 사용자)        │
│                                       │
│ 사용자별 ACL:                           │
│  admin    : (admin은 모든 권한)         │
│  mom      : [manage    ▼]   [×]        │
│  kid      : [hidden    ▼]   [×]        │
│                                       │
│ [+ 사용자 추가]                          │
│                                       │
│              [취소]  [저장]              │
└───────────────────────────────────────┘
```

### 5.3 P3 — 폴더 트리에서 ACL

폴더 탭 트리에서 폴더 우클릭 → "이 폴더 권한 설정" → 같은 모달 (folder_acl 행 편집).

ACL이 걸린 폴더는 트리에서 자물쇠 🔒 아이콘 표시.

### 5.4 P4 — 라이트박스 private 토글

상단 액션 바에 자물쇠 아이콘 추가:
- `inherit` (기본) → 🔓 아이콘 회색
- `private` → 🔒 아이콘 파랑 (본인+admin만 보임)
- `public` → 🌐 아이콘 (모든 사용자에게 read 강제, hidden root여도 노출)

토글 시 `PATCH /api/photos/{id}` `{visibility: ...}`. owner_user_id가 NULL이면 admin만 토글 가능.

### 5.5 P5 — 활동 로그 + 휴지통 격리

- **휴지통 탭**: 본인이 보낸 사진만 표시. admin은 "전체 보기" 토글 추가.
- **관리 → 활동 로그 탭 (신규)**: audit_log 테이블의 Tabulator-free HTML 표 (사용자/액션/리소스/시각 필터 + 페이지네이션).

---

## 6. 엣지 케이스 / 결정 사항

### 6.1 admin 우회
admin은 모든 ACL을 무시한다. `effective_level` 함수가 첫 줄에서 `if user.is_admin: return "manage"`.

### 6.2 사용자 삭제 시
- `users.id`가 사라지면 `root_acl`/`folder_acl`의 그 사용자 엔트리는 CASCADE로 함께 삭제 (이미 FK에 명시).
- audit_log의 `user_id`는 SET NULL되지만 `username`은 비정규화 보존.
- `photos.owner_user_id`는 SET NULL → 그 사진은 "주인 없음" → private 토글 권한 admin에게만 남음.
- `photos.trashed_by_user_id`도 동일.

### 6.3 root 삭제 시
관련 `root_acl`, `folder_acl`도 CASCADE 삭제 (이미 FK에 명시).

### 6.4 ML 워커 / 인덱싱 워커
워커는 모든 ACL을 우회한다 (시스템 작업). DB 직접 접근하므로 자동.

### 6.5 watcher (inotify)
watcher가 만드는 `discover_root` 잡은 시스템 잡 → ACL 무관.

### 6.6 검색 (text_q, tag_q 등)
검색은 visible_photo_ids subquery로 자동 필터됨. hidden 사진은 검색 결과에도 안 잡힘.

### 6.7 duplicates 탭
중복 그룹에서 hidden 사진은 빠진다. 그 결과 그룹이 1장만 남으면 중복 그룹에서 사라짐.

### 6.8 hidden 사진의 썸네일 캐시
브라우저가 이미 받은 썸네일 파일은 막을 방법이 없다. URL `/api/photos/{id}/thumb`이 401/404로 응답하므로 새 사용자에겐 안 보임. 하지만 admin이 ACL을 사후에 hidden으로 바꿔도 이미 사진을 본 사용자의 캐시까지는 회수 못 함. 이건 한계로 명시.

### 6.9 P3 path_prefix 매칭 시 길이 동률
같은 길이의 prefix가 충돌하면 `level` 우선순위(manage > contribute > interact > read > hidden)로 결정. 같은 사용자에 대해 같은 prefix는 PK 제약으로 중복 불가.

### 6.10 P4 photo.visibility=public이 root_acl=hidden을 이기는가?
**예**. public은 명시적 사용자 선택. hidden을 우회하려면 admin이 photo를 public으로 표시해야 함.

### 6.11 P5 audit_log 양
삭제·공유·ACL 변경 정도만 기록 → 가족용은 하루 수십~수백 행 수준. INDEX로 충분히 빠름. 90일 이상 자동 purge 옵션은 P5에서 같이 구현.

### 6.12 마이그레이션 롤백
각 alembic rev은 `downgrade()` 함수 작성. 단:
- P1 down은 데이터 손실 (사용자 플래그 컬럼 drop) → 경고 메시지.
- P2/P3 down은 ACL 테이블 drop → 모든 ACL 사라짐, 경고.
- P4 down은 visibility 컬럼 drop → private/public 잃음.
- P5 down은 audit_log drop, trashed_by 컬럼 drop.

**롤백 가이드라인**: down 적용 전 반드시 `data/catalog.db.snapshot` 백업.

---

## 7. Phase별 실행 체크리스트

### P1 — 사용자 플래그
- [ ] alembic 0012 작성 (users 4컬럼 + UPDATE)
- [ ] `app/models.py` — User에 4 필드 추가
- [ ] `app/auth.py` — `require_flag(flag)` 헬퍼
- [ ] mutating 라우터에 데코레이터 적용 (위 4.2 매트릭스 P1 컬럼 참고)
- [ ] 관리 → 사용자 UI: 체크박스 4개 (admin 비활성)
- [ ] `PATCH /api/admin/users/{id}`에 플래그 변경 지원
- [ ] 자동 테스트: viewer 사용자가 업로드/삭제/공유/메타편집 시도 → 403
- [ ] 문서: README에 "사용자 권한" 섹션 추가

**P1 완료 기준**: 기존 사용자 모두 동작 동일. 새 viewer 사용자 생성 시 모든 플래그 false, admin이 UI로 켤 수 있음.

### P2 — root ACL
- [ ] alembic 0013 작성 (root_acl 테이블)
- [ ] `app/models.py` — RootACL 모델
- [ ] `app/auth_acl.py` — `visible_photo_ids_subquery`, `effective_level`, `require_level`
- [ ] 위 4.2 매트릭스 P2 컬럼 라우터 일괄 수정
- [ ] 관리 → 사진 폴더 행에 "권한" 버튼 + ACL 모달
- [ ] `GET/POST/DELETE /api/admin/root_acl` 엔드포인트
- [ ] 자동 테스트: root_acl=hidden인 사용자가 list/locations/tags/folders/in-cell 모두 못 봄
- [ ] 문서: 관리 가이드에 root 권한 섹션

**P2 완료 기준**: 한 root를 특정 사용자에게 hidden 처리 시 그 사용자의 모든 갤러리 응답이 일관되게 비어있음. interact/contribute/manage 레벨도 mutating 작업으로 검증.

### P3 — folder ACL
- [ ] alembic 0014 작성
- [ ] `FolderACL` 모델 + `match_folder_acl()` 헬퍼
- [ ] `effective_level`에 folder 매칭 우선순위 추가
- [ ] 4.2 P3 컬럼 라우터 보강
- [ ] 폴더 트리에 자물쇠 아이콘 + 우클릭 메뉴
- [ ] `GET/POST/DELETE /api/admin/folder_acl` 엔드포인트
- [ ] 자동 테스트: root=read인데 하위 folder=hidden, root=hidden인데 하위 folder=read 둘 다
- [ ] 문서

### P4 — photo visibility
- [ ] alembic 0015 작성
- [ ] `photos.owner_user_id`, `photos.visibility` 모델 반영
- [ ] 업로드 시 `owner_user_id` 자동 채움
- [ ] `effective_level`의 visibility 처리
- [ ] 라이트박스 🔒 토글 + `PATCH /api/photos/{id}` visibility 필드
- [ ] 자동 테스트: private 사진은 owner+admin만, public은 hidden ACL 우회
- [ ] 문서

### P5 — 휴지통 격리 + 감사 로그
- [ ] alembic 0016 작성 (trashed_by + audit_log)
- [ ] 휴지통 보낼 때 `trashed_by_user_id` 채움
- [ ] `GET /api/admin/trash`에 본인 필터 (admin이면 `?all=true` 가능)
- [ ] audit 미들웨어 또는 헬퍼 `record(user, action, resource_type, resource_id, detail)`
- [ ] mutating 라우터 호출 후 audit 기록
- [ ] 관리 → 활동 로그 탭 신설 (HTML 표 + 필터: 사용자/액션/날짜)
- [ ] `GET /api/admin/audit?...` 페이지네이션
- [ ] 90일 이상 audit 자동 purge (worker / cron)
- [ ] 자동 테스트: viewer A가 삭제 → A의 휴지통에만 표시. 같은 작업이 audit에 기록.
- [ ] 문서

---

## 8. 자동 테스트 시나리오 (전 phase 누적)

각 phase마다 `tests/test_acl.py`에 통합 테스트를 누적한다:

```
fixtures:
  - 4 users: admin / mom (모든 플래그 true) / kid (모든 플래그 false) / guest (read only)
  - 2 roots: family / parents-private
  - photos: family/album1/foo.jpg, family/album1/bar.jpg, parents-private/secret.jpg
  - ACL setup per phase

P1 cases:
  - kid가 업로드 시도 → 403
  - kid가 삭제 시도 → 403
  - kid가 공유 생성 → 403
  - kid가 남의 평점 편집 → 403
  - kid가 본인 평점 추가 → 200

P2 cases (additive):
  - kid에 parents-private hidden 설정
  - kid의 /api/photos에 parents-private 사진 안 보임
  - kid의 /api/photos/{secret.jpg.id} → 404
  - kid의 /locations에 secret 없음
  - kid의 /tags에 secret만 가진 태그 안 보임

P3 cases (additive):
  - family root는 read, family/album1만 hidden for kid
  - kid의 갤러리에 album1 사진 안 보임, 다른 family 폴더 사진 보임

P4 cases (additive):
  - bar.jpg를 mom이 private으로 설정 (owner=mom)
  - kid는 bar.jpg 못 봄
  - mom은 봄
  - admin은 봄
  - foo.jpg를 admin이 public, family에 kid=hidden ACL → kid가 foo만 봄

P5 cases:
  - mom이 baz.jpg 삭제 → audit_log에 photo.trash 행 생김 (user=mom, resource=baz.id)
  - mom의 휴지통에 baz 보임
  - kid의 휴지통에 baz 안 보임
  - admin의 휴지통 `?all=true`에 baz 보임
```

---

## 9. 시간 추정 (확정)

| Phase | 백엔드 | 프런트 | 마이그/테스트/문서 | 합계 |
|---|---|---|---|---|
| P1 | 3h | 2h | 1h | **6h** |
| P2 | 8h | 4h | 4h | **16h** |
| P3 | 6h | 4h | 2h | **12h** |
| P4 | 4h | 3h | 1h | **8h** |
| P5 | 5h | 4h | 1h | **10h** |
| **합계** | | | | **52h** |

각 phase는 독립 PR. P1부터 순차 실행, 매 phase 끝에 사용자 검증·승인 후 다음 phase 진입.

---

## 10. 배포 노트

각 phase 적용 시 NAS 절차:

1. **백업 먼저** — `data/catalog.db.snapshot` 떠둠
2. 코드 받기 + 마이그레이션:

```bash
cd ~/myphotos && git pull && .venv/bin/python -m alembic upgrade head
```

3. API + 워커 재시작:

```bash
sudo systemctl restart myphotos-api myphotos-worker
```

4. 브라우저 강제 새로고침 (admin.html / index.html 둘 다 바뀜)

P1 외에는 데이터 마이그레이션 변화가 없어 다운타임 짧음 (~5초).

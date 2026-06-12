# MyPhotos 프론트엔드 스타일 가이드

PMS 프로젝트 스타일 가이드(JIO 디자인 시스템)를 MyPhotos에 맞게 번안한 문서.
MyPhotos는 **Bootstrap을 쓰지 않고**, **다크가 기본**이며 라이트 모드는
`body.light` 클래스로 오버라이드한다(PMS의 `[data-bs-theme="dark"]` 패턴과 반대).
정적 자산은 `app/web/static/` 에서 `/` 로 서빙된다.

---

## 1. 색상 (JIO 브랜드 팔레트)

| 용도 | 다크(기본) | 라이트(`body.light`) |
|------|-----------|----------------------|
| primary | `#5967ff` | `#323f53` |
| in-progress(진행중/부분) | `#4caaf0` | `#4caaf0` |
| error/지연/위험 | `#e42b2e` | `#e42b2e` |
| pending(대기) | `#d8dadb` | `#d8dadb` |
| 보조(skipped/제외) | `#6b7280` | `#6b7280` |

- **완료 = primary**, **진행중 = `#4caaf0`**, **지연/실패 = `#e42b2e`**, **대기 = 회색**.
- 같은 의미는 항상 같은 색(§9.2). 그라데이션 금지, 단색만(§9.1).

---

## 3. 차트 색상 통일

admin.html의 chart.js 도넛은 CSS 변수 `--chart-*` 로 색을 받는다
(`_CHART_TOKEN(k)` → `--chart-{k}`). 토큰은 브랜드 팔레트로 통일:

```css
:root {
  --chart-ok: #5967ff;       /* 완료 = primary */
  --chart-done: #5967ff;     /* 완료(잡) = primary */
  --chart-partial: #4caaf0;  /* 부분 = in-progress */
  --chart-running: #4caaf0;  /* 진행중 = in-progress */
  --chart-failed: #e42b2e;   /* 지연/실패 = error */
  --chart-pending: #d8dadb;  /* 대기 = grey */
  --chart-skipped: #6b7280;  /* 제외 = muted grey */
}
```

새 차트를 추가할 때도 색은 반드시 이 토큰을 통해 가져온다(하드코딩 금지).

---

## 4. 다크모드 지원

MyPhotos는 **다크가 기본값**이다. 라이트 모드는 `body.light` 로 오버라이드한다
(PMS의 `[data-bs-theme="dark"]` 와 방향이 반대임에 유의).

```css
.some-component { background:#1f1f1f; color:#eee; }      /* 다크(기본) */
body.light .some-component { background:#fff; color:#1a1a1a; }  /* 라이트 */
```

모든 신규 컴포넌트는 다크 기본 + `body.light` 오버라이드를 함께 정의한다.

---

## 5. 폰트 (Pretendard, 로컬)

```css
font-family: 'Pretendard Variable', 'Pretendard', -apple-system,
             BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
```

- 로컬 번들: `app/web/static/css/pretendard.min.css` + `app/web/static/fonts/pretendard/*.woff2`
- 각 페이지 `<head>` 에 `<link rel="stylesheet" href="/css/pretendard.min.css">` 로드
  (index / admin / login / share).
- 인트라넷/오프라인(NAS) 환경을 위해 CDN이 아닌 로컬 woff2 를 번들한다.
- **라이선스**: Pretendard는 **SIL Open Font License 1.1**(OFL-1.1). 번들·임베드·
  서빙·상업적 사용 모두 허용. 조건: 폰트 단독 판매 금지, 예약명 'Pretendard'
  유지, 라이선스 고지 동봉. 원문은 `fonts/pretendard/OFL.txt`.

---

## 8. 파일 구조

```
app/web/static/
├── css/
│   ├── pretendard.min.css   # Pretendard @font-face (로컬)
│   ├── gallery.css          # index.html 스타일
│   ├── admin.css            # admin.html 스타일
│   ├── login.css            # login.html 스타일
│   └── share.css            # share.html 스타일
├── fonts/pretendard/        # Pretendard woff2 (Regular~Black)
└── js/
    ├── common.js            # $/escape/_t/_tn + uiAlert/uiConfirm/uiPrompt(중앙 다이얼로그)
    ├── api.js               # api.* / friendlyError
    ├── i18n.js
    └── panels/              # lightbox / mapview / duplicates / trash / audit / shares
```

- 페이지별 대형 스타일은 인라인 `<style>` 대신 `css/*.css` 로 분리(§9.5).
- 공통 컴포넌트(다이얼로그) 스타일은 `common.js` 가 1회 주입한다(페이지 독립).

---

## 9. 스타일 적용 원칙

1. **그라데이션 금지** — 모든 UI는 단색.
2. **색상 일관성** — 같은 상태/유형은 같은 색(브랜드 팔레트 §1).
3. **CSS 변수/토큰 활용** — 하드코딩 대신 `--chart-*` 등 토큰 사용.
4. **다크모드 지원** — 다크 기본 + `body.light` 오버라이드를 항상 함께.
5. **인라인 스타일 최소화** — 대형 스타일은 `css/*.css` 로 분리.

---

## 부록: 공통 다이얼로그 (네이티브 대체)

브라우저 기본 `alert/confirm/prompt` 는 주소창 아래 상단에 고정되어 위치를
못 바꾼다. 대신 `common.js` 의 중앙정렬 모달을 쓴다.

```js
await window.uiAlert(message);                 // 알림 (Promise<void>)
const ok = await window.uiConfirm(msg, { danger: true });  // 확인 (Promise<boolean>)
const v  = await window.uiPrompt(msg, defaultValue);       // 입력 (Promise<string|null>)
```

- `window.alert` 는 중앙 모달로 자동 오버라이드된다(호출부 수정 불필요).
- `confirm/prompt` 는 동기 반환이라 자동 대체가 불가 → 호출부를 `await uiConfirm/uiPrompt` 로 변환한다.
- 삭제·영구삭제 등 파괴적 동작은 `{ danger: true }` (버튼 `#e42b2e`).
- 버튼색: primary `#5967ff`(다크)/`#323f53`(라이트), danger `#e42b2e`.

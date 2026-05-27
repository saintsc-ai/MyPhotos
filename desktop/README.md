# MyPhotos Desktop Client

Windows 데스크톱 래퍼. PySide6 + QWebEngine으로 기존 웹 프런트엔드를
임베드하고, PyInstaller로 단일 exe로 패키징합니다.

- **범위**: 뷰어 (타임라인 / 지도 / 폴더 / 주제 / 라이트박스 / 검색·필터).
  관리 페이지(`/admin*`)는 의도적으로 차단 — 그쪽은 브라우저에서 사용.
- **세션**: 로그인 쿠키는 `%APPDATA%\MyPhotos\qweb-storage\`에 영구
  저장. 두 번째 실행부터는 바로 갤러리로 들어갑니다.
- **서버 주소**: 첫 실행 시 입력 → `%APPDATA%\MyPhotos\config.json`에
  저장. 툴바의 **서버 변경** 버튼으로 언제든 바꿀 수 있음.

## 개발 환경에서 실행 (빌드 없이)

```powershell
cd desktop
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python app.py
```

## 빌드 (단일 exe)

```powershell
cd desktop
.\build.ps1
```

산출물: `desktop\dist\MyPhotos.exe` (대략 **180–220 MB** — Python +
Qt6 + Chromium까지 다 들어 있음). 첫 빌드는 5–10분, 이후는 캐시 덕에
빠릅니다.

`build.ps1`이 하는 일:

1. `.venv` 없으면 생성
2. `pip install -r requirements.txt` (PySide6 + PyInstaller)
3. `build\`, `dist\` 청소
4. `pyinstaller --clean myphotos.spec`

## 배포

`dist\MyPhotos.exe` 하나만 사용자에게 전달하면 됩니다. Python·Qt·기타
DLL 설치 불필요. 단일 파일이라 첫 실행 시 임시 폴더에 압축을 풀어
실행하는 데 몇 초 걸립니다 (이후 실행은 빠름).

> ⚠ Code signing 없음 → SmartScreen이 "Windows의 PC 보호" 경고를 띄울
> 수 있습니다. 추가 정보 → 실행으로 통과. 사내 배포면 EV 코드사이닝
> 인증서로 서명하면 사라지지만 별도 비용이 듭니다.

## 아이콘 (선택)

`desktop\icon.ico` 파일을 두면 exe 아이콘으로 들어갑니다. 적용하려면
`myphotos.spec`의 `# icon="icon.ico"` 주석 해제 후 다시 빌드.

## 알려진 함정

| 증상 | 원인 / 해결 |
| --- | --- |
| 첫 실행 시 페이지 빈 화면 | 서버 주소가 잘못됐거나 NAS가 꺼져있음. 툴바 **서버 변경**으로 다시 입력 |
| 로그인이 자꾸 풀림 | `%APPDATA%\MyPhotos\qweb-storage\` 권한 문제. 폴더 삭제 후 재실행하면 새로 만듦 |
| exe가 실행 안 됨 (콘솔이 깜빡임) | PyInstaller가 Qt 리소스를 빠뜨림. `myphotos.spec`의 `collect_all('PySide6')` 부분이 살아있는지 확인. UPX 압축 켜져 있으면 끄기 |
| 화면이 너무 작음/크게 보임 | Windows 디스플레이 스케일링 문제. 환경변수 `QT_AUTO_SCREEN_SCALE_FACTOR=1`로 실행 |

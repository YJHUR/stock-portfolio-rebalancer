# Stock Portfolio Dashboard

## 초보용 1분 실행
- 압축을 풀고 아래 경로에서 실행 파일을 찾습니다.
  - Windows: `dist/stockapp/stockapp.exe`
  - Linux: `dist/stockapp/stockapp`
- 위 실행 파일을 실행합니다.
- 잠시 후 브라우저에서 `http://localhost:60600`을 자동으로 엽니다.
- 자동으로 열린 브라우저에서 "페이지를 열 수 없음", "HTTPS 전용이 활성화된 HTTP URL" 같은 문구가 나오면 아래 순서로 수동 접속하세요.
  - 주소창에 `http://localhost:60600`을 직접 입력하고 Enter를 누릅니다.
  - 기본 포트(60600)가 아니라면 실행 창에 출력된 `Open this URL in your browser: http://localhost:포트/`의 포트 번호를 그대로 사용합니다.
  - Safari를 사용 중이면 경고 화면에서 `계속`을 눌러 로컬 주소 접속을 허용합니다.
- 화면 상단에서 포트폴리오를 만들고 계좌/종목/거래를 입력합니다.
- 종료는 실행 창을 닫거나 터미널에서 `Ctrl + C`를 누르면 됩니다.

개인/가족 단위 자산을 계좌별로 기록하고, 카테고리 목표 비중에 맞춰 리밸런싱 추천을 계산하는 Django 웹 앱입니다.

## 앱 목적

- 여러 계좌의 예수금/보유종목/거래내역을 한 화면에서 관리
- 카테고리별 목표 비중 대비 현재 비중을 시각화
- 그룹 단위 리밸런싱 추천(매수/매도 수량) 자동 계산
- 가격 보드(기준일/직전 영업일)로 변동 확인

## 핵심 기능

- 계좌/입출금/거래 입력
- 카테고리/리밸런싱 종목/종목목표비중 관리
- 그룹별 리밸런싱 상태 및 종목별 추천 수량 표시
- 모바일 화면 대응 반응형 대시보드
- 포트폴리오 탭 전환(멀티 포트폴리오 모드)

## 동작 방식

### 1) 데이터 모델

주요 엔터티:

- `Account`, `AccountSnapshot`, `CashFlow`
- `Stock`, `Category`, `HoldingSnapshot`, `PriceHistory`
- `RebalanceCategory`, `RebalanceStock`
- `Trade`

거래 저장 시 예수금과 보유 스냅샷이 함께 반영되며, 대시보드는 최신 스냅샷을 기준으로 계산합니다.

### 2) 리밸런싱 계산

- 카테고리 목표비중 기준으로 부족/초과 금액 계산
- 카테고리 내부는 종목 목표비중을 우선 반영
- 계좌별 종목 수가 불필요하게 늘지 않도록 계좌 선택 우선순위 적용
- 잔여 예수금 최소화를 위해 1주 단위 추가 매수 허용

### 3) 멀티 포트폴리오

- 상단 탭에서 포트폴리오 생성/전환/이름변경/삭제
- 포트폴리오별 SQLite DB 분리 저장
  - 레지스트리: `data/portfolios.json`
  - DB 파일: `data/portfolios/<portfolio-id>.sqlite3`

## 빠른 시작

### 1) 개발 실행(개발자용)

```bash
python3 -m pip install -r requirements.txt
python3 manage.py migrate
python3 manage.py runserver 0.0.0.0:92026
```

접속: `http://localhost:92026`

### 2) 무설치 실행(일반 사용자용)

이 방식은 Python 설치 없이 실행 파일만으로 앱을 사용하는 방법입니다.
`requirements.txt`를 사용자 PC에서 다시 설치하지 않습니다. (빌드 시 포함된 상태로 배포)

#### Windows 사용자

1. 배포 받은 압축 파일을 원하는 폴더에 풉니다.
2. 압축 해제 후 `dist/stockapp/stockapp.exe`를 더블클릭합니다.
3. 잠시 기다리면 브라우저가 자동으로 열리고 앱 화면이 나옵니다.
4. 브라우저가 자동으로 안 열리면 주소창에 `http://127.0.0.1:60600`을 입력합니다.
5. 종료할 때는 실행된 콘솔 창(검은 창)을 닫습니다.

#### Linux 사용자

1. 배포 받은 압축 파일을 원하는 폴더에 풉니다.
2. `dist/stockapp/stockapp` 파일을 실행합니다.
3. 실행 권한 오류가 나면 터미널에서 한 번만 아래를 실행합니다.

```bash
chmod +x stockapp
```

4. 실행 후 브라우저가 자동으로 열리지 않으면 `http://127.0.0.1:60600`으로 접속합니다.
5. 종료할 때는 실행한 터미널 창에서 `Ctrl + C`를 누릅니다.

### 3) 초기 데이터 입력 순서(권장)

1. 계좌 추가 (계좌명/그룹/예수금)
2. 카테고리 목표비중 입력
3. 리밸런싱 종목 및 종목 목표비중 입력
4. 거래/입출금 입력
5. 가격 업데이트 또는 수동 가격 입력

## 무설치 패키지 만들기(배포자용)

일반 사용자가 위 방식으로 실행하려면, 먼저 배포용 실행 파일을 빌드해야 합니다.

### 빌드

Linux:

```bash
bash scripts/build_linux.sh
```

Windows(Git Bash/WSL 등):

```bash
bash scripts/build_windows.sh
```

결과물:

- Linux: `dist/stockapp/stockapp`
- Windows: `dist/stockapp/stockapp.exe`

## 환경변수 (고급 설정)

- `STOCKAPP_DB_PATH`
  - 단일 DB 강제 모드(하위호환)
  - 예: `/tmp/stockapp-demo.sqlite3`
- `STOCKAPP_PORTFOLIO_REGISTRY`
  - 멀티 포트폴리오 레지스트리 경로 지정
- `STOCKAPP_PORT`
  - 런처 실행 시 기본 포트 변경

## 프로젝트 구조

- `config/` - Django 설정/URL
- `portfolio/` - 도메인 모델, 서비스 로직, 뷰, 템플릿
- `portfolio/management/commands/` - 관리 커맨드(현재 최소 유지)
- `launcher/` - 무설치 실행 런처
- `scripts/` - 빌드 스크립트
- `static/` - 정적 리소스

## 주의사항

- 개발 서버(`runserver`)는 HTTPS를 직접 처리하지 않습니다.
- 프로덕션 배포 시 WSGI 서버 + 리버스 프록시(Nginx 등) 구성을 권장합니다.

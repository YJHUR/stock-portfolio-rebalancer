# Windows Build via Cloud Agent (MCP)

이 문서는 "Windows 실행 파일(`dist/stockapp/stockapp.exe`)을 Cloud Agent + MCP로 빌드하고 커밋"할 때 쓰는 작업 지시서입니다.

## 목표

- Windows 환경에서 `bash scripts/build_windows.sh`를 실행
- 생성된 결과물(`dist/stockapp/stockapp.exe`)을 확인
- 변경사항을 커밋

## 에이전트 작업 지시사항

아래 순서대로 수행:

1. 저장소 상태 확인
   - 현재 브랜치, 변경사항, 최근 커밋 메시지 스타일 확인

2. Windows 빌드 실행
   - 명령: `bash scripts/build_windows.sh`
   - 빌드 실패 시 로그 원인 요약 + 수정 후 재시도

3. 산출물 검증
   - `dist/stockapp/stockapp.exe` 존재 확인
   - 파일 크기/수정시각 확인

4. 커밋
   - 빌드 산출물과 관련 변경만 스테이징
   - 커밋 메시지는 저장소 기존 스타일 준수
   - 커밋 후 `git status`가 깨끗한지 확인

5. 결과 보고
   - 실행한 빌드 명령
   - 산출물 경로
   - 커밋 SHA
   - 실패/주의사항(있다면)

## Cloud Agent 환경

- `.cursor/environment.json`에 Python/PyInstaller 의존성 설치 스크립트가 정의되어 있습니다.
- Cursor 관리형 Cloud Agent는 Ubuntu Linux에서 실행됩니다. Windows `.exe`는 GitHub Actions(`build-windows.yml`) 또는 Windows Self-Hosted Worker에서 빌드합니다.

## GitHub Actions (자동 Windows 빌드)

```bash
gh workflow run build-windows.yml
gh run list --workflow=build-windows.yml --limit 1
gh run watch <run-id>
```

빌드 완료 후 `dist/stockapp/stockapp.exe`가 레포에 커밋됩니다.

## 참고

- Linux 로컬 빌드는 `bash scripts/build_linux.sh`
- Windows 빌드는 Windows 환경(GitHub Actions `windows-latest` 또는 Self-Hosted Worker)에서 수행

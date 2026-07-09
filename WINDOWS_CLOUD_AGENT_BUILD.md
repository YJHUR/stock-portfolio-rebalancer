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

## 참고

- Linux 로컬 빌드는 `bash scripts/build_linux.sh`
- Windows 빌드는 로컬 Linux/macOS 셸이 아닌 Windows 환경에서 수행하는 것을 권장

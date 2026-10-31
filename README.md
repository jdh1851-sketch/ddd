# 끌림 매크로 Android 인수인계 — PC v0.12 기준

먼저 `handoff/PC_V012_TO_ANDROID.md`를 읽으세요. 예전 v0.9 이전 자료보다 이 문서가 최신입니다. 기존 Android 구현을 덮어쓰는 패키지가 아닌, 사용자가 확정한 요구사항과 PC에서 얻은 검증 결과를 전달하는 참고 소스입니다.

- `pc/kkullim_pc.py`: PC v0.12 실제 소스. Windows API/Tkinter 의존, Android 또는 Linux에서 그대로 실행 불가.
- `handoff/logic_reference.py`: 위 소스에서 추출한 플랫폼 독립 화면 상태/행동 결정 함수.
- `handoff/test_logic_reference.py`: 플랫폼 독립 분기 규칙 검사. `python -m unittest discover -s handoff -v`.
- `handoff/behavior.json`: 주요 설정과 시간 값.
- `assets/templates/`: PC 인식 이미지와 검색 영역. Android 좌표/이미지로 재검증할 것.
- `assets/icon.png`: 사용자가 선택한 앱 아이콘, 노란 100 배지 제거.
- `PC_사용설명서.md`: 참고 문서. 관리자 권한, B/6/Space/F8/F9를 Android에 그대로 요구하지 말 것.

Windows 실행 파일, 원본 동영상, 계정 정보, 사용자 실행 로그는 포함하지 않습니다. Android의 실제 APK 빌드/화면 인식/터치 검증은 기존 Android 채팅에서 이어갈 작업입니다.

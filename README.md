# 끌림 매크로 — Android 개발 인수인계

현재 PC 버전은 v0.9이며 Android 앱은 아직 구현하지 않았다. 이 폴더는 Codex 클라우드로 옮길 소스와 기획 자료이다. Windows EXE를 APK로 변환한 패키지가 아니다. 이 채팅이나 원본 동영상은 클라우드에 자동으로 전달되지 않는다.

## 포함 자료

- `pc/kkullim_pc.py`: 현재 Windows 구현. Windows API와 Tkinter에 의존하므로 Linux 클라우드에서 그대로 실행하지 말 것.
- `assets/templates/`: PC 영상에서 추출한 버튼/문구 참조 이미지와 검색 영역.
- `assets/icon.png`: 100 배지를 제거한 캐릭터 앱 아이콘.
- `PC_사용설명서.md`: PC 기능 설명. 관리자 권한, B/Space/6, F8/F9 등은 Android에서 그대로 적용하지 말 것.
- `ANDROID_SPEC.md`: 확정 요구사항과 Android에서 새로 검증할 사항.
- `클라우드_시작프롬프트.txt`: 다음 Codex 채팅에 전달할 작업 요청.

## 이전 상태

클라우드 업로드/환경 생성은 아직 완료되지 않았다. 조회 시 저장된 Codex 클라우드 환경이 없었다. 클라우드 환경의 프로젝트 파일에 이 폴더를 넣거나 해당 파일을 저장소에 올린 뒤 환경에 연결해야 한다. 새 클라우드 채팅을 실제로 시작하려면 사용자가 해당 환경을 선택해야 한다.

설정 위치: 설정 → Codex Cloud → Environments → Create environment.
공식 문서: https://learn.chatgpt.com/docs/environments/cloud-environments

## 개발 방향

클라우드에서는 Android 소스 작성, 빌드 환경 설정과 APK 빌드를 목표로 한다. 실제 설치, 화면 캡처 승인, 접근성 활성화 및 게임 내 입력 테스트는 Android 기기에서 직접 확인해야 한다. 클라우드에 프로젝트를 옮기는 일과 휴대폰 원격 제어는 별개이다.

먼저 실제 기기의 게임 화면 캡처와 터치가 동작하는 최소 테스트 앱부터 검증한다. 성공 전 무인 반복이 가능하다고 보장하지 않는다. 낚시는 별도 요구사항이 미확정이라 어비스 다음 단계로 남긴다.

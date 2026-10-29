# 원신 수집품 자동 마킹

Windows에서 원신 화면을 캡처하고 위치를 인식해 공식 티바트 지도에 가까운 미획득 수집품 하나를 표시하는 도구입니다. 게임 메모리를 읽지 않습니다.

## 설치와 첫 실행

1. [Releases](../../releases)에서 Windows ZIP을 받아 폴더 전체를 압축 해제합니다.
2. Windows 11 x64와 **Google Chrome**이 필요합니다. Python 설치는 필요 없습니다.
3. `GenshinCollectMarker.exe`를 실행하고 Windows 관리자 권한 요청을 승인합니다. 관리자 권한으로 실행 중인 게임의 키 입력을 읽기 위해 필요합니다.
4. 처음 열리는 **공식 티바트 지도에서 본인 계정으로 로그인**합니다. 앱이 로그인 여부를 읽기 요청으로 확인한 후 캡처 키를 활성화합니다. 지도 창을 닫아도 앱은 계속 실행됩니다.
5. 원신으로 돌아가 수집품을 획득한 뒤 아래 단축키를 누릅니다.

| 키 | 획득 종류 |
|---|---|
| `0` | 신의눈 |
| `-` | 보물상자·퍼즐 상자·타임어택 등 |
| `=` | 지역별 모라상자 |

단축키는 설정에서 바꿀 수 있습니다. 캡처는 게임 화면 또는 플레이어 화살표가 보이는 큰 지도에서 사용할 수 있습니다. 일반 게임 화면은 키보드/패드 UI와 원본/어두운 밝기를 최대4회 비교합니다. 최근 확인된 구역을 우선 검색하며 모호하면 미등록으로 보관합니다.

## 기능과 설정

- HDR FP16 캡처, SDR 흰색 역보정, 지상/지하 후보 구분. 지하의 세부 층 일치는 강제하지 않습니다.
- 가까운 미획득 핀1개를 표시하며 기본 거리 제한은 지도20단위(수평 약20m)입니다. 재시도는 이미 지정한 대상을 유지합니다.
- 미인식/미등록 큐: 선택 또는 전체 재시도·보관·삭제. 삭제는 공식 지도의 획득 표시 취소가 아닙니다.
- 캡처 대기시간, 알림 유지/페이드, 소리 on/off, 캐릭터 음성/캡처SE 개별 볼륨, 캐릭터별 사용 토글과 미리듣기.
- 성공 음성58개(17명), 실패3개, 캡처SE5개. 다음 대사 잔여음 제거와 끝부분 페이드가 적용돼 있습니다.
- 원본 캡처는 성공 처리 후 축소 보관하고, 실패 원본은21일 또는 원본총2GB 기준으로 정리합니다. 보관본/메타데이터는 유지합니다.

위치 인식은 추정이며 지도 데이터·지역·UI에 따라 실패할 수 있습니다. 첫 구역의 확대된 도시 미니맵은 특히 어려울 수 있습니다. 큰 지도에서는 플레이어 표시를 화면 안에 두세요. 게임 내 실제 상자 수와 공식 지도 핀의 묶음 단위는 다를 수 있습니다. 이 릴리스의 지도 데이터는 배포 시점 스냅샷입니다.

## 데이터와 업데이트

앱 폴더 안에 `capture-settings.json`, `captures/`, `official-browser-profile/`, `inspection/`이 생성됩니다. 로그인 정보는 전용 Chrome 프로필에 저장하며 배포판에 개인 계정 정보는 포함하지 않습니다. 이 폴더들을 다른 사람에게 전달하지 마세요.

업데이트 전 앱을 종료하고 새 배포판에 개인 설정·캡처·브라우저프로필을 옮기면 기록을 유지할 수 있습니다. 서로 다른 폴더의 앱을 동시에 실행하지 마세요. 소스/로그인 데이터/모든 캡처를 GitHub에 자동 업로드하는 기능은 없습니다.

진단은 `GenshinCollectMarker.exe --self-test`로 실행할 수 있습니다. 결과는 같은 폴더의 `self-test.json`에 기록됩니다. 게임 입력·획득 표시 없이 UI런타임,음원,네이티브인식기,HDR실행파일,임시Chrome프로필을 검사합니다.

## 소스에서 빌드

Windows 11 x64, Python3.12, Visual Studio2022 C++ Build Tools, Windows SDK22621 이상이 필요합니다.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt pyinstaller==6.22.3
```

1. `upstream-genshin-overlay/README.md`의 빌드 안내에 따라 vcpkg 의존성과 Release x64 인식기를 빌드합니다. 동봉한 소스는 기반 저장소의 수정본이며 빌드에는 해당 프로젝트의 vcpkg 구성이 필요합니다.
2. x64 Native Tools 명령 프롬프트에서 `cl /nologo /EHsc /std:c++17 build-work/capture-hdr.cpp /Fe:build-work/capture-hdr.exe /link d3d11.lib dxgi.lib user32.lib windowsapp.lib`로 HDR 캡처를 빌드합니다. SDK의 cppwinrt include 경로가 INCLUDE에 있어야 합니다.
3. `upstream-genshin-overlay/build/Release/map_data`가 없으면 `genshin-overlay.exe --make-index <map_data 폴더>`로 지도 데이터를 준비합니다. 공식 데이터 다운로드에 인터넷이 필요합니다.
4. `powershell -File build_release.ps1`로 실행파일과 필요한 자산만 패키징합니다. 이 과정에 개인 프로필·캡처·설정은 포함하지 않습니다.

## 라이선스와 출처

프로그램 소스는 GPL-3.0입니다. 위치 인식기는 [Jude-Chaer/genshin-overlay](https://github.com/Jude-Chaer/genshin-overlay)의 `63ba3eaf0e1ae008e35294ab217824c4dc6d1407`을 기반으로 합니다. 네이티브 라이브러리의 고지는 `THIRD_PARTY_LICENSES/`를 참고하세요.

원신, 지도 이미지와 캐릭터 음성은 HoYoverse 및 각 권리자의 콘텐츠이며 프로그램 코드의 GPL로 재허락되는 자산이 아닙니다. 음원별 영상 출처와 추출 시각은 `sounds/manifest.json`에 기록돼 있습니다. 본 도구는 HoYoverse의 공식 제품이 아닙니다.

# local-server — 라즈베리 파이 중앙 제어
전시에 들어가는 모든 라즈베리 파이(이하 Pi)를 중앙 컴퓨터 한 대에서 설치·실행·관리하고, AI들끼리 대화하는 쇼를 진행하는 시스템입니다. 같은 Wi-Fi(로컬 네트워크) 안에서 WebSocket으로 연결됩니다.

``` 인터융합2 전시 준비: 중앙 제어 시스템 ```

> **설치 순서만 보려면 → [SETUP.md](SETUP.md)** (중앙 라즈베리 파이 설치, 각 Pi 설정값, 작품 올리기, 안 될 때)

## AI 글 엄청 헷갈리니까... 직접 쓴 설명
개발 셋업:  
문제 상황: 각 라즈베리 파이에 지금 minjeong 폴더처럼 live api와 함께 2인치 디스플레이에 재생될 애니메이션이 들어갈거야. 이 애니메이션은 다 creative code 방식일거고. 그리고 live-api 구조 자체도 다 조금씩 다르고, persona, api key도 각자가 갖고 있어.  
문제는 이 많은 rpi에 소프트웨어를 2명이 다 셋업을 해줘야 하거든 (지금은 rpi os 설치까지 되어있는 상태야). 그래서 하나하나 하기 어려우니까 중앙제어시스템을 만들어서 거기에다가 각각 업로드?를 해주면 그게 셋업이 돼서 돌아가고, 전시 관리도 하고, 자동으로 켜지기도 하고, API Key도 거기서 집어넣고, 하고 싶어. 로컬 네트워크 사용할거고, websocket으로.  
requirements: 각 rpi에 어쨌든 중앙제어컴퓨터에서 보내는 프로그램을 받을 수 있는 sw를 깔긴 해야 하잖아? 그걸 공수가 가장 덜 들게 가장 쉽게 되어야 하고.  
그리고 중앙서버에 ui로 된 관리 대시보드가 필요해.  
  
UX:  
idle에서는 마스터만 마이크가 켜져있어.   
유저가 "00에 대해 대화해봐"라고 하면  
그 마스터가 특정 rpi에게 (이거 랜덤으로 할 수도 있고 대시보드에서 정할 수도 있게) 그 오디오 패킷을 넘기면서 사용자가 말한 것을  repeat을 하고 그에 대한 대답을 하라고. (가령 받는 rpi가 말투가 냥 으로 끝나는 말투면 그 rpi가 지금 우리 보러 온 사람이 00에 대해 대화해보라고 했다냥. 나는 이런거같다냥. 이렇게 말하는거야.)
이때부터는 다른 rpi들도 다 마이크가 켜져서 그 냥 rpi의 말을 각자의 마이크로 듣고 막 왁자지껄 대화를 하는거지.  
그 다음에 유저가 그만이라고 말하면 다들 rpi 세션을 죽여서 조용히 만들고.   


## 왜 필요한가
- 각 Pi에는 live api 음성 에이전트 + 2인치 디스플레이에 나오는 애니메이션(creative code)이 같이 돌아감 (`minjeong/` 폴더 같은 형태)
- 근데 학생마다 live api 구조가 다 조금씩 다르고, persona도 API 키도 각자 가지고 있음
- 이걸 Pi 수십 대에 2명이 하나하나 들어가서 세팅하는 건 불가능에 가까움
- 그래서: **Pi에는 “받아서 실행하는 프로그램(agent)”만 딱 한 번 깔고, 나머지는 전부 중앙 대시보드에서** 함 (이 에이전트는 AI가 아니라 설치 마법사같은거)
  - 프로젝트 보내기(Deploy) → 자동 설치 → 자동 실행
  - 전원 켜면 알아서 켜짐, 죽으면 알아서 다시 켜짐
  - API 키, 프롬프트도 대시보드에서 넣음
  - 전시 중에 Start / Stop / Restart, 로그 보기

## 한눈에 보는 구조

```text
                         같은 Wi-Fi
                             │
                 ┌───────────────────────┐
                 │  중앙 컴퓨터 (맥)       │
                 │  controller/server.py │  ← 브라우저로 대시보드 접속
                 └───────────┬───────────┘
                             │ WebSocket (명령·상태·로그)
           ┌─────────────────┼─────────────────┐
           │                 │                 │
       Pi (master)        Pi (agent)        Pi (agent)      ...
       agent.py           agent.py          agent.py        ← 각 Pi에 한 번만 설치
          │ bridge           │ bridge          │ bridge
       진행자 페이지        학생 A 페이지       학생 B 페이지     ← Chromium 전체화면
          │                 │                 │
       Gemini Live       Gemini Live       Gemini Live      ← 각 Pi가 자기 키로 직접 연결
```

세 부분으로 나뉨:

| 이름 | 어디서 돌아감 | 하는 일 |
| --- | --- | --- |
| controller | 중앙 컴퓨터 | 대시보드, Pi 목록, 프로젝트 보내기, 쇼 진행 |
| agent | 각 Pi (systemd로 자동 실행) | 중앙과 연결 유지, 프로젝트 설치·실행·재시작, 키/프롬프트 파일 만들기 |
| live-bridge.js | 각 학생 페이지 안 | agent가 “마이크 켜/꺼/이 말 해”를 페이지에 전달하는 통로 |

중요한 점: **중앙은 학생 코드를 이해하려고 하지 않음**. 코드가 다 다르니까. 대신 “어떻게 실행하는지”만 적힌 작은 설명서(manifest)를 보고 자동으로 실행함. 그리고 쇼에 참여하려면 페이지가 `start / stop / say` 세 가지 함수만 bridge에 알려주면 됨.

---

## 전시 인터랙션 (UX)

### 흐름

1. **Idle**: 마스터 Pi만 마이크가 켜져 있음. 나머지 Pi는 세션이 꺼져 있어서 조용함 (애니메이션만 돌아감)
2. 관람객이 마스터에게 **“AI들아 00에 대해 대화해봐”** 라고 말함
3. 마스터가 그 말을 알아듣고 중앙에 “대화 시작, 주제 00” 이라고 알림
4. 중앙이 첫 발언자 Pi 하나를 고름 (**랜덤** 또는 **대시보드에서 지정**)
5. 첫 발언자에게 관람객이 한 말을 넘기면서 “이걸 네 말투로 전하고 네 생각을 말해” 라고 시킴
   - 예: 말투가 “냥”으로 끝나는 Pi면 → *“지금 우리 보러 온 사람이 00에 대해 대화해보라고 했다냥. 나는 이런 거 같다냥.”*
6. 1.5초 뒤(설정 가능) **다른 모든 Pi도 마이크가 켜짐** → 냥 Pi의 말을 각자 자기 마이크로 듣고 반응 → 왁자지껄 대화
7. 관람객이 **“그만”** 이라고 하면 → 마스터가 중앙에 알림 → 모든 agent Pi의 세션을 끔 → 다시 조용해지고 1로 돌아감
   - 대시보드의 **그만** 버튼, 그리고 **최대 대화 시간**(기본 180초)이 지나도 자동으로 그만

```text
관람객 ──말──▶ 마스터 Pi ──start_chat(주제, 한 말)──▶ 중앙
                                                    │
                        ┌───── kickoff(지시문) ──────┤ 첫 발언자 선택
                        ▼                           │
                  냥 Pi 스피커 ──소리(공기)──▶ 다른 Pi들 마이크 ◀── wake (1.5초 뒤)
                                                    
관람객 ──"그만"──▶ 마스터 Pi ──stop_chat──▶ 중앙 ──sleep──▶ 모든 agent Pi
```

### 기술적으로 어떻게 되는가
- 마스터는 Gemini Live의 **function calling(도구)** 을 씀. `start_chat(topic, utterance)`, `stop_chat()` 두 개를 등록해두면, 관람객이 “대화해봐” / “그만” 이라고 할 때 모델이 알아서 도구를 호출함. 페이지는 그걸 받아서 `LiveBridge.startChat()` / `LiveBridge.stopChat()` 만 부르면 됨. ([예제 코드](examples/demo-agent/app.js), [진행자 프롬프트](examples/demo-agent/host.txt))
- 첫 발언자에게는 관람객 말이 **텍스트**로 들어감 (`realtimeInput.text`). 지시문은 대시보드 “쇼 설정”에서 고칠 수 있음 (`{topic}`, `{utterance}` 자리에 들어감)
- 다른 Pi들끼리는 **네트워크로 대화를 주고받지 않음**. 진짜로 스피커 → 공기 → 마이크로 들음. 그래서 전시장에서 “진짜 대화하는 것처럼” 보임

### 오디오 패킷 대신 텍스트로 넘기는 이유
처음에는 관람객 목소리 오디오를 그대로 첫 발언자에게 넘기는 걸 생각했는데, MVP는 텍스트로 넘김.
- 오디오를 넘기면 받는 Pi의 VAD가 “사람이 나한테 말했다”로 받아서 바로 대답해버림. “이걸 전달해줘”라는 지시를 붙이기가 어려움
- 마스터가 이미 관람객 말을 알아들었으니(도구 호출할 때 `utterance`에 그대로 들어옴), 텍스트로 넘기면 “관람객이 이렇게 말했어, 네 말투로 전해” 라고 감싸서 보낼 수 있음
- 나중에 “관람객 실제 목소리를 재생”하는 연출이 필요하면 마스터가 녹음한 PCM을 같은 경로(`kickoff`)로 넘기는 걸 추가하면 됨

### Considerations
- **동시에 말하기**: 모든 Pi가 동시에 듣고 있어서 한 명이 말하면 여러 명이 동시에 대답할 수 있음. 왁자지껄이 목표라 어느 정도는 괜찮지만, 너무 시끄러우면 persona에 “다른 친구가 말하는 중이면 기다린다”, “1~2문장만” 같은 걸 넣어야 함. 대시보드 카드에 각 Pi의 `mic: speaking / listening` 이 실시간으로 보이니까 보면서 조정
- **에코(AEC)**: 크롬 AEC는 “내 스피커에서 나온 내 소리”만 지워줌. 다른 Pi 소리는 안 지워지는데, 이건 오히려 원하는 동작(들어야 하니까). 대신 Pi끼리 너무 가까우면 서로 소리가 뭉개질 수 있어서 배치 간격 테스트 필요
- **마스터도 AI들 대화를 들음**: 대화 중에 마스터가 AI 말에 반응하면 안 됨. 그래서
  - 대화 중에는 마스터 소리 출력을 끔 (예제에서 `mode === "chat"`이면 재생 안 함)
  - 대화 중에 마스터가 `start_chat`을 또 부르면 중앙이 무시함
  - AI가 “그만”이라는 단어를 말해서 마스터가 착각할 수도 있음 → 대시보드 그만 버튼 + 최대 대화 시간으로 안전장치
- **API 사용량**: 대화 중에는 Pi 개수만큼 Live 세션이 동시에 열림. 하나의 키를 여러 Pi가 같이 쓰면 동시 세션 제한(특히 free tier)에 걸릴 수 있음. 학생 각자 키를 쓰는 게 안전함
- **세션 준비 시간**: 마이크를 켜는 건 = Live 세션을 새로 여는 것이라 1초 정도 걸림. 그래서 첫 발언자가 말을 시작하는 동안 다른 Pi들이 준비됨 (`others_delay`)

---

## 폴더 구조

```text
local-server/
├── controller/            ← 중앙 컴퓨터
│   ├── server.py          대시보드 + WebSocket 허브 + 쇼 진행
│   ├── install-central.sh 중앙을 라즈베리 파이로 쓸 때 설치 (자동 실행 등록)
│   ├── url.sh             대시보드 주소 / Pi 설치 명령 다시 보기
│   ├── start.command      맥에서 실행 (더블클릭 또는 sh)
│   ├── static/            대시보드 화면 (html/css/js, 빌드 없음)
│   └── data/              (주의) 자동 생성. 토큰·API 키·업로드 파일. git에 안 올라감
├── agent/                 ← 각 Pi
│   ├── agent.py           중앙 연결, 프로젝트 설치·실행·감시
│   └── install.sh         Pi 최초 설치 스크립트 (중앙이 주소/토큰 채워서 내려줌)
├── bridge/
│   └── live-bridge.js     학생 페이지에 넣는 파일
└── examples/
    ├── demo-agent/        bridge에 맞춘 가장 작은 예제 (agent/master 둘 다 됨)
    └── pi-check/          Pi 점검용 (화면·마이크·스피커·쇼 신호, Gemini 키 없이)
```

---

## 1. 중앙 컴퓨터 켜기

Python 3.10 이상. 저장소 루트에서:

```bash
sh local-server/controller/start.command
```

첫 실행은 가상환경을 만들고 `aiohttp`를 설치함. 터미널에 이렇게 뜸:

```text
  대시보드:  http://192.168.0.10:8700/?t=3f9c1a...
  Pi 설치:   curl -fsSL "http://192.168.0.10:8700/install.sh?t=3f9c1a..." | bash
```

- 대시보드 주소를 브라우저로 열면 됨 (같은 Wi-Fi의 다른 노트북에서도 됨). 한 번 열면 쿠키가 저장돼서 다음부터는 `?t=` 없어도 됨
- `?t=...` 는 토큰. 이게 없으면 아무도 접속·제어 못 함. **토큰 들어간 주소는 톡방 같은 데 올리지 말기**
- 중앙 컴퓨터 IP가 바뀌면 Pi들이 못 찾아옴 → 공유기에서 중앙 컴퓨터를 **DHCP 고정(reservation)** 해두기. 또는 `--public-url http://내맥이름.local:8700` 으로 mDNS 이름 사용
- 포트 변경: `sh local-server/controller/start.command --port 8800`

### 중앙을 라즈베리 파이로 할 때
```bash
bash local-server/controller/install-central.sh
```
- 부팅하면 자동으로 켜짐 (systemd `live-control`). 이름이 `raspberrypi`면 `live-control`로 바꾸고, Pi들은 `http://live-control.local:8700`으로 찾아옴 → 공유기가 바뀌어서 IP가 달라져도 그대로 됨
- 주소 다시 보기: `bash local-server/controller/url.sh` / 로그: `journalctl -u live-control -f`
- 중앙은 3초마다 같은 네트워크에 "나 여기 있음" 신호(UDP 8701)를 보냄. Pi가 저장된 주소로 못 붙으면 이 신호로 중앙을 다시 찾아감 (토큰으로 서명돼서 가짜 중앙으로는 안 붙음)

## 2. 라즈베리 파이 추가 (Pi마다 딱 한 번)

OS는 설치되어 있는 상태에서, Pi를 같은 Wi-Fi에 연결하고 터미널에서:

```bash
curl -fsSL http://live-control.local:8700/join | bash
```

**아무것도 안 물어봄.** 끝나면 대시보드 위쪽에 **연결 요청**이 뜸 → **허용** 누르면 그 Pi가 목록에 뜸 (토큰을 Pi로 옮길 필요 없음).

- 중앙이 아직 없으면: `bash pi-setup.sh` (중앙에서 `python3 local-server/controller/server.py --make-setup pi-setup.sh`로 만든 파일, agent가 안에 들어 있음). 중앙이 켜지면 Pi가 알아서 찾아가서 연결 요청함
- 허용 없이 바로 붙이고 싶으면 예전처럼 토큰 포함 명령 (`…/install.sh?t=토큰`, 대시보드 "허용 없이 바로 붙이는 명령")
- 설치하면 Pi에 `live` 명령이 생김: `live`(상태) / `live run ~/작품폴더`(중앙 없이 바로 띄우기) / `live stop` / `live key`(API 키) / `live logs`

이 설치가 하는 일:

1. `python3-venv`, `chromium`, `unclutter`(마우스 커서 숨기기), `pulseaudio-utils`(대시보드에서 스피커·마이크 설정), `fonts-nanum`(한글 글꼴) 설치
2. 중앙에서 `agent.py`, `live-bridge.js`를 받아서 `~/.live-agent/`에 넣음
3. `live-agent` systemd 서비스 등록 → **부팅하면 자동 실행, 죽으면 3초 뒤 자동 재시작**
4. 데스크톱 자동 로그인 켜기, 화면 꺼짐 끄기 (Chromium이 화면에 뜰 수 있게)
5. Wi-Fi 절전 끄기(음성 끊김 방지), 시간대가 UTC면 한국 시간으로

끝나면 한 번 `sudo reboot`.

그 다음부터는 Pi에 키보드/마우스 연결할 일이 없어야 함.

- 같은 명령을 다시 실행해도 안전함 (설정·프로젝트 유지). 뭔가 꼬이면 그냥 다시 실행
- 이름은 처음엔 Pi hostname으로 뜸. 대시보드에서 클릭해서 바꾸면 됨 (예: `03-냥이`)
- 어떤 게 어떤 Pi인지 모르겠으면 카드의 **Identify** → 그 Pi 화면이 5초 동안 노란색 + 삑 소리
- 대시보드에 안 뜨면 Pi에서: `journalctl -u live-agent -f`
- 기기 구분은 보드 고유번호(시리얼)로 함 → SD카드를 복제해도 대시보드에 따로 뜸. 복제했으면 이름(hostname)만 Pi마다 바꾸기 (SETUP.md 팁 참고)
- 2인치 화면 드라이버(`/boot/firmware/config.txt`)와 스피커·마이크 선택은 설치 명령이 못 해줌 → SETUP.md "손으로 해야 하는 것"

## 3. 대시보드 사용법

기기 카드 하나 = Pi 한 대.

1. **역할** 선택: `agent`(일반 학생 Pi) / `master`(관람객 말 듣는 진행자 Pi, 보통 1대)
2. **프로젝트** 선택: 이 repo의 학생 폴더들 + `examples/` + zip 업로드한 것들이 목록에 나옴
3. **API 키**: “설정 · 프롬프트 · 로그” 열어서 그 Pi 전용 키 입력. 비워두면 “공용 API 키” 사용
4. **Deploy** → 중앙이 폴더를 zip으로 묶어서 Pi로 보냄 → Pi가 받아서 설치 → 자동 실행
   - **제일 쉬운 방법: 카드에 작품 폴더나 zip을 끌어다 놓기** (또는 "zip 올리기"/"폴더 올리기") → 업로드 + 이 Pi의 프로젝트로 지정 + Deploy가 한 번에 됨. 같은 이름으로 다시 올리면 새 버전으로 교체, Pi가 꺼져 있으면 켜질 때 자동 설치
   - 한글 파일명(맥 zip의 자모 분리, 윈도우 zip의 CP949)도 안 깨지게 풀어줌
   - Pi가 GitHub에 접속할 필요 없음. 중앙 컴퓨터에 있는 파일이 그대로 감. 그러니까 **중앙 컴퓨터 repo를 최신으로 pull 한 다음 Deploy**
   - `.env`, `key.js`, `config.local.js`, `.venv` 같은 건 안 보냄 (키는 대시보드에서 넣은 걸로 새로 만듦)
5. **프롬프트**: “기기에서 불러오기” → 고치고 → “저장 + 재시작”. Deploy를 다시 해도 대시보드에서 바꾼 프롬프트가 유지됨. “원래 프롬프트로” 누르면 프로젝트 파일 원본으로 돌아감
6. **로그**: 프로젝트 서버 출력 + 페이지의 `console.warn/error` 가 실시간으로 보임. Pi에 모니터 안 꽂고 디버깅 가능
7. **오디오**: 이 Pi의 기본 스피커·마이크 선택, 볼륨/감도, 스피커 테스트(띵동), 마이크 테스트(2초 녹음해서 레벨 표시). 바꾼 뒤 작품 Restart

카드의 뱃지:
- `RUNNING / STOPPED / INSTALLING / ERROR / EMPTY(프로젝트 없음)`
- `mic: sleep / listening / speaking` — 페이지가 bridge에 연결돼 있을 때만. `no bridge`면 그 페이지는 쇼에 참여 못 함

**전체 제어**: Start all / Stop all / Restart all / Deploy all, Agent 업데이트(전체 Pi의 agent.py를 새 버전으로), Reboot all, 전원 끄기 all (전시 끝날 때)

**작품 올리기 (목록에만 추가)**: 여러 Pi가 같이 쓸 작품을 미리 올려둘 때. zip이나 폴더를 올리면 목록에 `업로드 · 이름`으로 생김 (이름 비우면 파일/폴더 이름). zip 안에 폴더가 하나만 있으면 알아서 벗겨냄

**Pi 점검**: 카드에서 `예제 · pi-check`를 Deploy → 화면에 이름, 마이크 막대, Identify 삑, 쇼 신호까지 한 번에 확인

---

## 4. 학생이 할 일 — 내 프로젝트를 중앙 제어에 맞추기

전부 다 할 필요는 없고, 1~2만 해도 Deploy해서 돌아가기는 함. 쇼(AI끼리 대화)에 참여하려면 3까지 해야 함.

### 1) API 키는 코드에 쓰지 말고 파일 하나에서 읽기
중앙이 Pi에 키 파일을 자동으로 만들어줌. 그러니까 코드는 **그 파일에서 키를 읽기만** 하면 됨. 제일 쉬운 규칙:

```html
<script src="key.js"></script>   <!-- window.GEMINI_KEY = "..." -->
```

- 폴더에 `key.example.js`가 있으면 중앙이 알아서 `key.js`를 만들어줌 (Eunseol, sunny, minseo는 이미 이 방식)
- `config.example.js`가 있으면 `config.local.js`를 만들어줌 (ParkSoyeon 방식)
- `server.py`가 있으면 `.env`에 `GEMINI_API_KEY=` 를 만들어줌 (master, Yunjung 방식)
- **화면에 키를 입력하는 칸만 있는 프로젝트**(HuhGaeun, seoyoungchae)는 Pi에서 아무도 못 입력함 → `key.js`에서 읽도록 바꾸기
- **코드에 키가 박혀 있으면 안 됨!!** (minjeong의 `heol.html` 안에 키가 들어있음 → 지우고 key.js 방식으로)

### 2) 프롬프트(persona)는 파일로 빼기
지금 대부분 프롬프트가 `const SYSTEM_INSTRUCTION = \`...\`` 처럼 코드 안에 있음. 이러면 대시보드에서 못 바꿈. 이렇게 바꾸기:

```text
내폴더/
├── index.html
├── app.js
└── persona.txt    ← 프롬프트는 여기
```

```js
// 세션 시작할 때 읽기
const persona = await (await fetch("persona.txt", { cache: "no-store" })).text();
// setup: { systemInstruction: { parts: [{ text: persona }] }, ... }
```

`persona.txt` 또는 `prompt.txt`가 있으면 대시보드 프롬프트 편집이 자동으로 켜짐.

### 3) live-bridge.js 붙이기 (쇼 참여용)
[`bridge/live-bridge.js`](bridge/live-bridge.js)를 내 폴더에 복사하고:

```html
<script src="live-bridge.js"></script>
```

그리고 내 코드에서 **세 가지 함수만** 알려주면 됨:

```js
LiveBridge.connect({
  // 세션 열고 마이크 켜기. setupComplete 받을 때까지 기다렸다가 끝나야 함 (async)
  start: async () => { await openSession(); },
  // 세션 닫고 마이크 끄기
  stop: async () => { closeSession(); },
  // 열린 세션에 텍스트 보내기 — 첫 발언자가 될 때 쓰임
  say: (text) => { ws.send(JSON.stringify({ realtimeInput: { text } })); },
});
```

선택 (있으면 대시보드에서 보기 좋음):

```js
LiveBridge.speaking(true);   // 모델 목소리 재생 시작할 때
LiveBridge.speaking(false);  // 재생 끝났을 때
LiveBridge.ended();          // 세션이 에러 등으로 혼자 끊겼을 때
LiveBridge.on("mode", (m) => { /* m.mode: "idle" | "chat" — 애니메이션 바꾸기 등 */ });
```

- 맥에서 혼자 개발할 때는 agent가 없으니 bridge는 조용히 아무것도 안 함. 기존 “대화 시작” 버튼 그대로 쓰면 됨
- Pi에서는 “대화 시작” 버튼을 누를 사람이 없음. bridge가 `start()`를 대신 불러줌. 그래서 **start()가 버튼 클릭 없이도 돌아가야 함** (Chromium은 마이크 권한·오디오 자동재생을 허용한 상태로 켜짐)
- `server.py` 방식(master, Yunjung)은 CSP 헤더 `connect-src 'self'` 때문에 bridge 연결이 막힘 → `connect-src 'self' ws://127.0.0.1:8765` 로 바꾸기
- 완성된 예시는 [`examples/demo-agent/app.js`](examples/demo-agent/app.js). 대시보드에서 `예제 · demo-agent` 를 Deploy해보면 바로 써볼 수 있음

### 실행 스크립트(.sh) 하나로 된 작품도 됨
`eunseol.sh`처럼 파일을 풀고 서버·Chromium을 직접 켜는 스크립트를 zip으로 올려도 돌아감:
- 대시보드 API 키가 `GEMINI_API_KEY`와 `GEMINI_KEY` 두 이름으로 다 들어감 → 스크립트가 키를 묻지 않고 바로 씀
- 스크립트가 켜는 Chromium에는 전시용 옵션이 자동으로 붙음 (마이크 권한 자동 허용, 소리 자동 재생, 키링·오류 창 끄기)
- 스크립트는 `wait`로 계속 떠 있는 게 제일 좋음. `start.sh`처럼 띄워 놓고 바로 끝나는 방식도 됨 (띄운 서버·브라우저가 살아 있는 동안 그대로 둠)
- zip 안에 작품 파일이 **전부** 들어 있어야 함 (start.sh만 있으면 안 됨)

### 2인치 화면에 띄우는 방법 (agent가 알아서)
- 작품 켜기 직전에 agent가 화면 목록(`wlr-randr`, X11이면 `xrandr`)을 읽고 **제일 작은 화면**을 고름
- 작품이 켜는 Chromium(스크립트가 켜는 것 포함)에 그 화면의 위치·크기(`--window-position`, `--window-size`)를 붙이고, Wayland(labwc)에서는 창 위치를 정할 수 있게 `--ozone-platform=x11`(Xwayland)로 띄움 → 전체화면이 2인치 화면에서 됨
- 작은 화면이 모니터 왼쪽에 있으면 맨 오른쪽으로 옮겨 둠 (창이 잠깐 크게 떠도 모니터 쪽으로 넘어가지 않게)
- 대시보드: 방향(0/90/180/270, 전체 + Pi별), "모니터 끄고 2인치만" (작품 켤 때 모니터 끄고 Stop하면 다시 켬), Pi별 화면 직접 고르기
- 모니터를 안 꽂은 전시 상태에서는 화면이 2인치 하나뿐이라 그냥 거기 뜸

### 4) (선택) live.json — 실행 방법 설명서
안 써도 중앙이 폴더 모양을 보고 추측함:

| 폴더에 있는 것 | 추측하는 실행 방법 |
| --- | --- |
| `server.py` (+ `requirements.txt`) | venv 만들고 설치 → `server.py --port 8000` → Chromium으로 `localhost:8000` |
| `*.sh` 스크립트 | 그 스크립트 실행 (화면도 스크립트가 알아서 띄움. minjeong 방식) |
| `index.html` 등 html만 | `python3 -m http.server 8080` → Chromium으로 `localhost:8080` |

추측이 틀리면 폴더에 `live.json`을 쓰거나, 대시보드 카드의 manifest 칸에서 직접 고침:

```json
{
  "name": "냥이",
  "setup": "",
  "run": ["python3 -m http.server 8080 --bind 127.0.0.1"],
  "browser": { "url": "http://localhost:8080/", "args": [] },
  "files": { "key.js": "window.GEMINI_KEY = \"${GEMINI_API_KEY}\";\n" },
  "prompt_file": "persona.txt"
}
```

| 키 | 뜻 |
| --- | --- |
| `setup` | Deploy 후 처음 한 번만 실행 (패키지 설치 등). 실패하면 ERROR |
| `run` | 계속 켜져 있어야 하는 명령들. 죽으면 자동 재시작 |
| `browser` | Chromium 전체화면(kiosk)으로 띄울 주소. `args`에 창 위치/크기 같은 옵션 추가 |
| `files` | Pi에서 자동으로 만들 파일. `${GEMINI_API_KEY}`, `${DEVICE_NAME}`, `${PROJECT_DIR}` 치환 |
| `prompt_file` | 대시보드에서 편집할 프롬프트 파일 |

### 5) 2인치 디스플레이 / 애니메이션
- 애니메이션도 그냥 같은 페이지 안의 canvas/p5 등으로 그리면 됨. Chromium이 kiosk로 띄워줌
- 화면이 여러 개거나 회전이 필요하면 `minjeong`처럼 직접 실행 스크립트(`run_xxx.sh`)를 만들고 manifest `run`에 넣기. 이때 `browser`는 `null`
- 페이지가 받을 수 있는 상태: `mode`(idle/chat), 내 `speaking`, 세션 켜짐 여부 → 이걸로 “자는 얼굴 / 듣는 얼굴 / 말하는 얼굴” 같은 연출 가능

### 지금 각 폴더 상태

| 폴더 | 키 | 프롬프트 | 해야 할 일 |
| --- | --- | --- | --- |
| master, Yunjung | `.env` (OK) | `persona.txt` (OK) | bridge 붙이기 + CSP 수정 |
| Eunseol, sunny | `key.js` (OK) | 코드 안 | persona.txt 빼기, bridge |
| minseo | `key.js` (OK) | 화면에서 만듦 | 기본 프롬프트를 persona.txt로, bridge |
| ParkSoyeon | `config.local.js` (OK) | 코드 안 | persona.txt 빼기, bridge |
| HuhGaeun, seoyoungchae | 화면 입력칸 (X) | 화면 입력칸 | key.js·persona.txt에서 읽기, bridge |
| minjeong | 코드에 박힘 (X) | 코드 안 | 키 삭제 → key.js, 폴더로 풀어서 올리기, bridge |
| Hayeon | — | — | 아직 README만 있음 |

---

## 통신 규칙 (개발자용)

### 중앙 ↔ Pi agent (`ws://중앙:8700/ws/device?id=…&t=토큰`)

| 방향 | type | 내용 |
| --- | --- | --- |
| Pi → 중앙 | `/ws/pair?id=…&name=…` (토큰 없음) | 연결 요청. 대시보드에서 허용하면 `{type: paired, token}`, 거절하면 `pair_denied` (1분 뒤 다시 요청) |
| Pi → 중앙 | `hello` / `status` | 5초마다: 상태, 프로젝트, 온도, load, IP, bridge 연결 수, mic 상태, 화면 목록·작품 화면, 기본 스피커·마이크 |
| Pi → 중앙 | `log` | 로그 한 줄 |
| Pi → 중앙 | `result` | 명령 결과 `{req, command, ok, message, data}` |
| Pi → 중앙 | `event` | 페이지에서 온 쇼 이벤트 `start_chat` / `stop_chat` |
| 중앙 → Pi | `config` | 이름, 역할, env(API 키), 프롬프트, `screen {target, rotate, only}` — 연결될 때마다·바뀔 때마다 (screen이 바뀌면 작품 다시 켬) |
| 중앙 → Pi | `deploy` | `{path: "/bundle/<id>.zip", manifest}` → Pi가 HTTP로 zip을 받아감 (큰 파일이라 WebSocket 대신) |
| 중앙 → Pi | `start` `stop` `restart` `get_prompt` `get_logs` `identify` `update_agent` `reboot` `poweroff` | |
| 중앙 → Pi | `audio` | `{op: get}` 장치·볼륨 목록 / `{op: set, sink, source, sink_volume, source_volume}` / `{op: test, what: speaker\|mic}` (pactl) |
| 중앙 → 모두 | UDP 8701 브로드캐스트 | 3초마다 `{live_control, port, sig}` — Pi가 중앙 주소를 못 찾을 때 다시 찾는 용도 |
| 중앙 → Pi | `voice` | 페이지로 그대로 전달할 메시지 (아래) |

### `live` 명령 ↔ Pi agent (`http://127.0.0.1:8765/local/…`, 헤더 `X-Live-Local: 1` 필요)

`GET status`, `GET logs`, `POST run {path}`, `POST key {key}`, `POST stop|start|restart` — Pi 안에서만 열림. 헤더가 없으면 거절 (작품 페이지가 몰래 부르지 못하게)

### Pi agent ↔ 페이지 (`ws://127.0.0.1:8765/bridge`)

| 방향 | type | 뜻 |
| --- | --- | --- |
| agent → 페이지 | `hello` | `{id, name, role}` |
| agent → 페이지 | `wake` / `sleep` | 세션+마이크 켜기 / 끄기 → `start()` / `stop()` |
| agent → 페이지 | `kickoff` | `{text}` → `start()` 후 `say(text)` |
| agent → 페이지 | `mode` | `idle` / `chat` (페이지가 새로 연결될 때도 지금 값을 보내줌) |
| agent → 페이지 | `identify` | 화면에 이름 표시 |
| 페이지 → agent | `voice_state` | `sleep` / `listening` / `speaking` |
| 페이지 → agent | `event` | `start_chat {topic, utterance}` / `stop_chat` |
| 페이지 → agent | `log` | 대시보드 로그로 |

### 주요 설계 결정
- **왜 Pi가 중앙에 접속하나 (반대가 아니라)**: Pi IP는 계속 바뀌고 몇 대인지도 모름. Pi가 중앙 주소 하나만 알고 먼저 접속하면 새 Pi를 추가할 때 중앙 설정이 필요 없음
- **왜 GitHub pull이 아니라 중앙이 파일을 보내나**: 학생 PR이 merge 안 된 상태에서도 테스트 가능, Pi에 git 인증 필요 없음, zip 업로드도 같은 경로로 처리. source of truth는 중앙 컴퓨터의 repo
- **왜 중앙이 꺼져도 Pi는 돌아가나**: 마지막 프로젝트·키·프롬프트를 Pi(`~/.live-agent/state.json`)에 저장해둠. 중앙은 “관리용”이고, 전시 중에 중앙 컴퓨터가 잠들어도 각 Pi는 계속 대화함 (쇼 진행만 멈춤)
- **왜 Gemini 연결을 중앙에서 안 하나**: 오디오를 전부 중앙으로 모으면 네트워크·중앙 컴퓨터가 병목이 됨. 각 Pi가 자기 키로 직접 연결
- **연결 요청(허용)**: 토큰을 Pi로 옮기는 게 제일 귀찮아서 만듦. 허용 = 그 Pi에 토큰을 줌. 처음 연결할 때는 Pi가 중앙이 진짜인지 확인할 방법이 없음 → 전시용 공유기에서만 쓰기. 한 번 연결된 뒤에는 토큰으로 서명된 신호만 믿음
- **보안 범위**: 토큰 하나로 대시보드·Pi 연결을 다 막음. 대신 토큰이 있으면 모든 Pi에서 아무 명령이나 실행할 수 있는 것과 같음 → 관람객이 쓰는 Wi-Fi 말고 **전시용 공유기를 따로** 쓰는 걸 추천. HTTPS는 안 씀 (로컬 네트워크 전용)

## 개발 순서 (MVP)

1. Pi 1대 + `examples/demo-agent` → 대시보드에서 online, Start/Stop/Restart, 재부팅 후 자동 실행 확인
2. Pi 1대를 `master`, 2대를 `agent`로 → demo-agent로 “대화해봐 → 왁자지껄 → 그만” 흐름 확인
3. 학생 프로젝트 하나씩 1~3 적용해서 Deploy
4. 전시장에서 배치 간격, 볼륨, `others_delay`, 최대 대화 시간, persona 길이 조정

**MVP 완료 기준**: Pi 3대에서 각각 install 명령을 한 번만 실행한 뒤, Pi에 키보드/마우스 없이 중앙 대시보드만으로 서로 다른 학생 프로젝트를 배포·실행·재시작하고, 키와 프롬프트를 바꾸고, 로그를 보고, 마스터에게 말해서 AI끼리 대화를 시작/종료할 수 있다. Pi 전원을 껐다 켜도 자동으로 다시 실행되고 대시보드에 다시 붙는다.

## 제한과 다음 단계
- 실제 Pi 하드웨어·실제 Gemini 키로는 아직 검증 안 함. 리눅스에서 가짜 Pi 여러 대(가짜 Chromium, 실제 PulseAudio)로 설치 스크립트 → 업로드(zip/폴더, 한글 파일명) → 설치·실행 → 키/프롬프트 → 쇼 시작/그만 → 오디오 설정·테스트 → 재시작 후 자동 복구 → 중앙 주소 바뀜 → Agent 업데이트까지 확인함 (aiohttp 3.8 / 3.14 둘 다)
- 대화 순서 제어(한 번에 한 명만 말하기)는 없음. 필요하면 `speaking` 상태를 이용해서 중앙이 “지금 말하는 Pi 말고는 잠깐 대기” 신호를 보내는 식으로 추가
- 관람객 원본 목소리 전달(오디오 kickoff)은 없음 (위 설명 참고)
- 대시보드 로그인은 토큰 하나뿐. 여러 명이 권한을 나눠 쓰는 기능 없음

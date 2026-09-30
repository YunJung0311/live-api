# Master · Gemini 3.8 Live 기준 구현

## 빠른 실행

가장 간단한 실행: 저장소 루트에서 `sh master/start.command`. 첫 실행은 가상환경·의존성과 `.env` 템플릿을 만든다. `.env`에 키를 입력하고 같은 명령을 다시 실행한다. 기존 `.env`는 덮어쓰지 않는다. Chrome에서 `http://localhost:8000/`을 연다. 포트 변경은 `sh master/start.command --port 8001`. 설치 실패 시 아래 pip 명령을 다시 실행한다. 프런트엔드 빌드·npm·DB는 필요 없다.

Python 3.10 이상과 최신 Chrome을 권장한다. 저장소 루트에서 실행한다.

```bash
python3 -m venv master/.venv
master/.venv/bin/python -m pip install -r master/requirements.txt
cp master/.env.example master/.env
```

`master/.env`에 Google AI Studio에서 발급한 키를 넣는다.

```dotenv
GEMINI_API_KEY=여기에_실제_키
```

서버를 시작한다.

```bash
master/.venv/bin/python master/server.py
```

Chrome에서 `http://localhost:8000/`을 열고 **대화 시작**을 누른다. 포트가 사용 중이면 다음처럼 바꾼다.

```bash
master/.venv/bin/python master/server.py --port 8001
```

## 동작 구조

```text
마이크
  ↓ 브라우저 AEC / NS / AGC
AudioWorklet → 연속 16kHz PCM 변환 → binary WebSocket
                                         ↓
브라우저 ← 자막·24kHz PCM ← loopback server ← Gemini Live API
   ↓
재생 queue → 같은 페이지의 스피커

끼어들기: 로컬 RMS가 먼저 queue를 비움 → 서버 interrupted가 확정 → 잔여 조각 억제 해제
```

API 키는 `server.py`만 읽는다. 브라우저 HTML, JavaScript, URL, `localStorage`에는 키가 들어가지 않는다. 서버는 `127.0.0.1`에만 열리고 allowlist의 다섯 화면 파일만 제공한다.

## 설정과 범위

설정은 **대화 시작 시 한 번** 검증되어 Gemini setup에 들어간다. Live API의 session configuration은 연결 중 임의 변경할 수 없으므로 값을 바꾸려면 세션을 다시 시작한다.

| UI 설정 | 실제 전달 위치 | 범위 / 기본값 | 선택 이유 |
| --- | --- | --- | --- |
| Voice | `generationConfig.speechConfig` | 공식 30개 / Kore | 학생별 음색 비교를 완전히 커버 |
| Top P | `generationConfig.topP` | 0.01–1 / 생략 | 후보 확률 누적합 실험 |
| Top K | `generationConfig.topK` | 실험 1–100 / 생략 | 모델별 지원 확인 필요 |
| Presence / frequency penalty | 같은 이름의 generationConfig 필드 | 실험 −2–2 / 생략 | 토큰 반복 실험 |
| Temperature | `generationConfig.temperature` | 생략 또는 0–2 / 생략 | 기본 동작을 보존하되 HuhGaeun 실험 범위 수용 |
| Max output tokens | `generationConfig.maxOutputTokens` | 생략 또는 1–65,536 / 생략 | 모델 한도까지 검증하되 음성 길이는 우선 persona로 제어 |
| Start sensitivity | automatic VAD | HIGH/LOW / HIGH | 2개 enum 전체 |
| End sensitivity | automatic VAD | HIGH/LOW / HIGH | 2개 enum 전체 |
| Prefix padding | automatic VAD | 0–500ms / 20ms | API는 int32지만 음성 실험에 유효한 범위만 노출 |
| Silence duration | automatic VAD | 100–2,000ms / 700ms | 공식 권장 500–800ms를 기본에 반영 |
| Activity handling | `realtimeInputConfig` | interrupt/no-interrupt / interrupt | 원 과제 기본은 barge-in, 성격 실험용 예외 제공 |
| 입·출력 전사 | setup top-level | 각각 on/off / on | 자막이 필요 없으면 비용 절감 가능 |
| 시스템 지시문 | `systemInstruction` | 0–8,000자 / `persona.txt` | 학생별 persona 실험. 빈 값은 검증된 기본 사용 |
| 로컬 RMS | 브라우저만 | 0–0.3 / 0.08 | 서버 이벤트보다 먼저 재생을 끊는 장치별 보정값 |
| 캐릭터 도우미 | 문장으로 바꿔 systemInstruction 뒤에 추가 | 다정함·유머 0–100, 목표 1–10문장, 답변 언어 | 생성 문장 미리보기; API 성격 수치가 아님 |
| 이전 대화 이어가기 | `sessionResumption.handle` | 마지막 복구 핸들 | 탭 메모리만 사용; 새로고침하면 사라짐 |
| 내 말 끝났어요 | `realtimeInput.audioStreamEnd` | 버튼 이벤트 | 자동 VAD 상태에서 말끝을 직접 알림 |

Top K·penalties의 범위는 앱의 실험 범위이지 공식 3.8 한도가 아니다. 선택값은 비우면 전송하지 않는다. 특히 topK는 모델에 따라 허용하지 않는다. 거절되면 값을 비우고 다시 연결한다. [자세한 설정 설명](PARAMETERS.md)을 참고한다.

`contextWindowCompression`은 UI로 노출하지 않고 25,000 tokens에서 작동해 8,000 tokens를 남기도록 고정했다. 시스템 지시문은 유지되고 오래된 대화만 제거된다.

## 주요 설계 결정

### 왜 브라우저가 Google에 직접 연결하지 않는가

장기 API 키를 브라우저에 주면 소스, 개발자 도구, WebSocket URL, 저장소 실수로 노출될 수 있다. Google도 client-to-server 연결에는 ephemeral token 사용을 권고한다. 이 과제는 Python 서버가 이미 허용되므로 더 단순한 server-to-server 중계를 선택했다.

### 왜 AudioContext를 16kHz로 강제하지 않는가

장치와 브라우저가 요청 sample rate를 그대로 제공한다는 보장은 없다. Worklet은 실제 `sampleRate`를 받아 44.1/48kHz block 경계에서도 이어지는 area-average 방식으로 16kHz PCM을 만든다. 출력은 각 응답 MIME의 rate를 읽으며, 없을 때만 Live API 기본 24kHz를 사용한다.

### 왜 로컬 VAD가 턴 종료를 결정하지 않는가

로컬 RMS는 스피커 잔향·전시장 소음에 민감해 재생 queue를 빠르게 비우는 데 사용한다. 자동 VAD를 기본으로 유지하고 ‘내 말 끝났어요’ 버튼으로 audioStreamEnd를 실험한다. 마이크는 계속 켜져 있고 다음 오디오가 스트림을 다시 연다. 자동 hybrid 감지기나 완전 manual VAD를 구현한 것은 아니다. 응답 유지(NO_INTERRUPTION)를 선택하면 로컬 RMS 중단도 끈다.

### 캐릭터 슬라이더와 API 값을 어떻게 구분하는가

‘캐릭터 만들기 — 프롬프트 생성용’을 켜면 숫자와 양 끝의 의미를 담은 문장을 기본 지시문 뒤에 추가한다. 생성 문장을 그대로 보여준다. 80은 40의 두 배 효과라는 뜻이 아니며 목표 문장 수도 강제 길이가 아니다. 기본 지시문과 상충하는 문장은 편집해서 정리한다. 직접 지시문 8,000자와 도우미 문장 최대 2,000자를 서버에서 검증한다.

### 왜 일부 API 기능은 생략했는가

- 샘플링은 선택 입력으로 추가했고 기본값은 모두 생략한다.
- session resumption은 버튼으로 추가했다. 마지막 체크포인트 이후 발화는 유실될 수 있다. 자동 재시도·영구 저장은 하지 않는다. UI는 핸들 수신 시점부터 보수적으로 2시간 만료를 사용한다. 복구가 실패하면 새 대화를 시작한다. 종료 후 값을 바꾸고 이어가면 새 설정으로 복구 요청한다.
- tools/function calling: 외부 동작이 없는 음성 대화 범위 밖이다.
- video/media resolution: 원 과제가 audio-only다.
- 전사 고급 설정은 전사 전용 모델의 기능과 3.8 대화 모델의 지원이 달라 보류했다. [설정 해설](PARAMETERS.md)에서 구분한다.

## 모델 선택

사용자 요청에 따라 `gemini-3.8-live`로 갱신했다. 기존 학생 제출은 변경하지 않았다.

3.8은 thinkingLevel을 받지 않으므로 UI와 thinkingConfig를 제거했다. Proactive audio는 항상 켜져 있고 false를 보내면 오류다. Affective dialog 설정은 제거되었다. 주변의 무관한 말에 침묵하는 것은 가능한 동작이다. 응답은 AUDIO, 화면 글은 output transcription으로 받는다. [공식 migration](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-live)

## 파일

| 파일 | 역할 |
| --- | --- |
| `server.py` | 키 로드, 설정 검증, 정적 allowlist, Gemini WebSocket 중계 |
| `index.html`, `style.css` | 반응형 설정·대화 UI |
| `app.js` | 세션 수명, 마이크, 자막, 로컬 끼어들기 |
| `audio.js`, `pcm-worklet.js` | 연속 리샘플링, PCM 변환, 재생 queue와 flush |
| `persona.txt` | 빈 UI 지시문 대신 쓰는 기본 persona |
| `test_server.py`, `audio.test.mjs` | 중계·보안·설정·오디오 검증 |

## 테스트

실제 키 없이 실행된다.

3.8 변경 후 Python 8개·Node 오디오 6개 테스트가 통과했다. 모의 Google 서버로 샘플링 값, 복구 핸들과 audioStreamEnd의 실제 전송 JSON을 확인했다. 브라우저에서 캐릭터 도우미의 프롬프트 미리보기도 확인했다. 실제 Gemini 키·마이크 통화는 아직 검증하지 않았다.

```bash
master/.venv/bin/python -m unittest discover -s master -p 'test_*.py' -v
node --test master/audio.test.mjs
```

실제 장치에서는 별도로 다음을 확인한다.

- 3턴 이상 연결 유지
- 스피커 소리에 자기 응답이 반복되지 않는지
- 답변 중 말했을 때 체감 중단 지연
- 500/700/800ms silence duration 비교
- 전시장 소음에서 로컬 RMS 오탐·미탐
- 세션 종료 후 마이크 표시가 사라지고 다시 시작되는지

## 제한과 다음 추가 시점

- 자동 세션 재연결: 버튼 복구로 부족한 상시 전시가 필요할 때
- 자동 hybrid/manual VAD: 말끝 버튼 실험 후 실제 소음·지연 측정이 있을 때
- ephemeral token: 중계 서버 없이 브라우저가 Google에 직접 연결해야 할 때
- Raspberry Pi의 별도 AEC: Chromium AEC와 물리 배치 조정으로 해결되지 않을 때

## 공식 문서

- [Live API capabilities](https://ai.google.dev/gemini-api/docs/live-api/capabilities)
- [WebSocket API reference](https://ai.google.dev/api/live)
- [Live API best practices](https://ai.google.dev/gemini-api/docs/live-api/best-practices)
- [Gemini 3.8 Live model](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-live)
- [TTS voice options](https://ai.google.dev/gemini-api/docs/speech-generation#voices)

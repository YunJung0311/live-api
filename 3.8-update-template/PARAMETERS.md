# 학생용 파라미터 해설

확인일: 2026-09-16. Gemini Developer API 기준이다. 공통 스키마의 필드와 모델별 지원은 다르다. 특히 **3.8 Live 음성 대화**와 **3.5 Transcribe 전사**를 구분한다.

## Top P / Top K / 반복 벌점

AI는 다음에 나올 작은 조각인 토큰을 후보 중에서 고른다. 토큰은 단어나 단어 일부일 수 있다. 아래는 원리를 설명하는 가상 확률이다.

| 후보 | 사과 | 배 | 포도 | 귤 | 기타 |
| --- | --- | --- | --- | --- | --- |
| 확률 | 40% | 30% | 15% | 10% | 5% |

**Top K는 후보 개수**다. K=2면 사과·배만 남긴다. K=1이면 가장 유력한 후보 하나다. 응답 길이나 검색 개수가 아니다. 모델의 getModel 결과에서 topK가 비어 있으면 요청에 topK를 넣을 수 없다.

**Top P는 누적 확률**이다. P=0.7이면 사과·배로 70%를 채운다. P=0.9면 포도까지 85%이므로 귤까지 포함한다. 정답률 90%라는 뜻이 아니다. 질문마다 후보 개수가 달라진다.

**Temperature는 뽑기의 치우침**이다. 낮으면 유력한 후보로, 높으면 덜 유력한 후보도 선택할 여지가 커진다. 높다고 더 정확하거나 똑똑해지는 것은 아니다. [GenerationConfig](https://ai.google.dev/api/generate-content#v1beta.GenerationConfig)

**Presence penalty는 “이미 썼니?”**, **frequency penalty는 “몇 번 썼니?”**다. 양수면 이미 쓴 토큰을 다시 선택하기 어렵게 한다. Presence는 첫 사용 이후 횟수와 무관하고 frequency는 횟수에 비례한다. 예를 들어 값 0.3, 사용 횟수 4라면 개념적으로 각각 0.3과 1.2만큼 점수에 작용한다. 퍼센트포인트를 빼는 계산이 아니다.

0은 벌점 없음, 음수는 반복을 장려한다. 큰 음수 frequency는 출력 한도까지 반복하게 만들 수 있다. 토큰 제어이므로 “같은 주제를 절대 반복하지 말라”를 보장하지 않는다. [반복 벌점 정의](https://ai.google.dev/api/generate-content#v1beta.GenerationConfig)

Master는 모든 선택값을 비워 기본 동작을 먼저 확인하게 했다. 한 번에 한 값만 바꾸면 차이를 설명하기 쉽다. Top K 1–100과 벌점 −2–2는 앱의 실험 범위이지 공식 3.8 최대치가 아니다. 실제 모델이 거절하면 값을 비운다. 공통 Live setup에 필드가 있어도 지원과 효과는 호출로 확인해야 한다. [Live setup](https://ai.google.dev/api/live)

## Session resumption — 대화의 책갈피

전화가 끊겨 다시 걸어도 이전 이야기를 이어가는 기능이다. setup에 `sessionResumption: {}`를 넣으면 서버가 복구 지점을 보내고, 재연결 때 `sessionResumption: {handle: "받은 값"}`을 보낸다. 핸들은 사용자가 정하는 값이 아니라 서버가 발급하는 불투명한 책갈피다.

공식 문서는 연결 수명이 약 10분이고 복구 토큰은 마지막 세션 종료 후 2시간 유효하다고 설명한다. Compression은 대화 문맥의 크기를 관리하고 resumption은 연결을 바꾸며 대화를 잇는다. [세션 관리](https://ai.google.dev/gemini-api/docs/live-api/session-management)

책갈피는 전체 녹음이 아니다. 모델이 답변 중인 순간처럼 `resumable: false`일 때 이전 핸들을 사용하면 최근 대화가 빠질 수 있다. Master는 마지막 핸들을 탭 메모리에 보관하고 버튼으로 복구한다. 새로고침하면 사라진다. 재연결 때는 모델을 제외한 설정을 바꿀 수 있다. [복구 상태·설정 변경](https://ai.google.dev/api/live)

## VAD — 말할 차례 정하기

VAD는 말소리 감지다. 언제 시작하고 끝났는지 판단한다. 무슨 말을 했는지 글로 적는 전사와는 역할이 다르다.

| 방식 | 시작 | 끝 | 신호 |
| --- | --- | --- | --- |
| Automatic | Google | Google | 오디오 계속 전송 |
| Hybrid | Google | 앱; Google이 fallback | `audioStreamEnd: true` |
| Manual | 앱 | 앱 | `activityStart: {}` / `activityEnd: {}` |

Automatic은 침묵 시간을 기다린다. Hybrid는 앱이 말끝을 먼저 발견하면 종료 신호로 기다림을 줄인다. 너무 민감하면 “저는… 음… 사과가 좋아요”의 생각하는 시간을 말끝으로 잘라버린다. Manual은 자동 감지를 끄고 앱이 양쪽 경계를 책임진다. 버튼으로 시작·종료를 정할 수도 있지만 늦게 감지하면 첫 음절을 잃을 수 있다. 수동 감지기의 말끝 침묵은 공식 문서가 최소 500ms를 권장한다. [VAD 가이드](https://ai.google.dev/gemini-api/docs/live-api/capabilities#hybrid-vad)

`audioStreamEnd`는 숫자 설정이 아니라 지금 보내는 이벤트다. 자동 VAD가 켜져 있을 때 사용하고 다음 오디오가 스트림을 다시 연다. 자동 감지를 껐다면 `activityEnd`를 사용한다. [메시지 규칙](https://ai.google.dev/api/live)

Master의 ‘내 말 끝났어요’는 사람이 누르는 종료 보조다. 자동 hybrid 감지기나 완전 manual VAD 구현은 아니다. 로컬 RMS는 별도로 재생을 끊는 장치 보정값이다. 소리 크기만 보므로 말소리와 박수를 완전히 구분하지 못한다.

## 전사 — 들리는 말을 글로 적기

현재 Live transcription 가이드는 받아쓰기 모델 `gemini-3.5-transcribe-live`를 설명한다. Master의 `gemini-3.8-live`는 듣고 답하는 대화 모델이다. 아래 옵션을 3.8에서 모두 지원한다고 해석하면 안 된다. [모델 역할 비교](https://ai.google.dev/gemini-api/docs/live-api/live-transcribe)

**Language hints:** “한국어와 영어가 나올 거야”라는 힌트다. 예: `languageCodes: ["ko-KR", "en-US"]`. 비우면 자동 감지다. 번역이나 AI 답변 언어 설정은 아니다.

**Custom vocabulary:** 작품명·이름처럼 오인식하기 쉬운 표현을 알려준다. 예: `["별빛 정원", "김영채"]`. 비슷한 발음 중 이 표현을 고려하도록 유도하며 철자 강제나 새 지식 학습은 아니다. 전사 가이드는 최대 1,000개, 보통 100개 이내의 관련 용어를 권장한다. [언어·용어 설정](https://ai.google.dev/gemini-api/docs/live-api/live-transcribe#parameter-reference)

**Timestamps:** “안녕”이 녹음 시작 후 1.2–1.6초에 있었다는 시간표다. 단어 단위와 발화 단위가 다르다. 화면 도착 시각도 정확한 녹음 시각과 다르다. 현재 Live 가이드는 단어 단위 timestamp를 지원하지 않는다고 명시한다.

**Diarization:** “화자 A: 안녕 / 화자 B: 반가워”처럼 목소리를 구분한다. A의 실제 이름을 알아내는 신원 확인 기능은 아니다. Live streaming에서는 지원하지 않으며 파일 전사에서 사용한다. [Live 제약](https://ai.google.dev/gemini-api/docs/live-api/live-transcribe#limitations)

**SMART mode:** 원문을 읽기 좋은 받아쓰기로 정리한다. “어… 사과 두 개, 아니 세 개 주세요”를 “사과 세 개 주세요”처럼 바꿀 수 있다. 반복·말더듬·자기수정을 정리하고 문장과 목록을 다듬는다. VERBATIM은 원래 표현을 보존하는 방향이다. 말더듬을 연구한다면 SMART가 필요한 정보를 지울 수 있다. 지능이나 추론 수준을 올리는 설정은 아니다.

파일 전사는 SMART와 단어 timestamp·diarization을 함께 사용할 수 없다. 현재 파일 전사 문서는 custom vocabulary와 단어 timestamp·diarization 조합도 거절한다고 명시한다. [파일 전사 모드와 제약](https://ai.google.dev/gemini-api/docs/transcribe)

Master는 기본 입출력 전사 on/off를 제공한다. 언어 힌트·용어·SMART는 관련성은 있지만 3.8 지원 확인이 먼저다. 별도 전사 모델 연결을 추가하면 코드 흐름과 비용도 늘어나므로 이번 구현에서는 보류했다. 답변 언어는 프롬프트 도우미로 지정한다.

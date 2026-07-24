# dishell (Discord Antigravity Remote Shell)

디스코드 개인 메시지(DM)를 통해 로컬 PC에서 실행 중인 Antigravity CLI(`agy`) 세션을 원격 제어하고 진행 상황을 모니터링하는 셀프호스팅 에이전트.

## 기능
- Antigravity CLI 구동 및 로컬 입출력 프록시 기능
- `/rc on`, `/rc off` 슬래시 커맨드를 통한 디스코드 원격 제어 세션 토글 기능
- 실시간 스트리밍 출력을 버퍼링하여 디스코드 메시지로 전송
- 디스코드 DM 프롬프트를 Antigravity CLI에 입력으로 전달

## 설치 및 실행
1. `requirements.txt` 설치
   ```bash
   pip install -r requirements.txt
   ```
2. `.env` 파일 설정 (`.env.example` 참고)
3. 봇 실행
   ```bash
   python main.py
   ```

## 사용법
- 로컬 CLI에서 `agy` 대신 `python main.py`를 실행합니다.
- `main.py` 프로세스의 콘솔에서 `/rc on`을 입력하면 디스코드와 연동됩니다.
- 디스코드 봇에게 DM으로 메시지를 보내면 `agy` 세션에 명령어/프롬프트가 전달됩니다.
- `/rc off`를 입력하면 연동이 해제됩니다.
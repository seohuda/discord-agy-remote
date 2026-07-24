# discord-agy-remote

**discord-agy-remote**은 로컬 환경에서 실행 중인 Antigravity CLI(`agy`) 세션을 디스코드 개인 메시지(DM)를 통해 원격 제어 및 모니터링할 수 있도록 지원하는 셀프 호스팅 에이전트 브릿지입니다.

기존 `agy` CLI의 워크플로우를 방해하지 않으며, 필요 시점에만 디스코드 봇을 동적으로 구동하여 리소스 낭비 없이 양방향 통신(Bidirectional I/O)을 제공합니다.

## 핵심 기능 (Features)

* **Seamless Native Integration**: 설치 스크립트를 통해 시스템의 `agy` 명령어를 래핑하여, 사용자는 기존과 완전히 동일한 CLI 경험을 유지할 수 있습니다.
* **Dynamic Bot Lifecycle**: `agy` 세션 내부에서 `/rc on` 커맨드 입력 시 디스코드 봇이 백그라운드에서 동적으로 연결되며, `/rc off` 입력 시 연결을 안전하게 해제하여 무한 루프가 가능하도록 설계되었습니다.
* **Real-time Output Streaming**: Antigravity CLI의 `stdout`을 실시간으로 버퍼링 및 청크(Chunk) 단위로 분할하여 디스코드 DM으로 안정적으로 전송합니다.
* **Remote Command Injection**: 디스코드 DM을 통해 전송된 메시지를 로컬 `agy` 프로세스의 `stdin`으로 즉각 파이프(Pipe) 처리하여 로컬 터미널과 동일한 수준의 제어를 지원합니다.

## 시스템 요구사항 (Prerequisites)

* Python 3.8 이상
* Antigravity CLI (`agy`) 로컬 설치 환경
* Discord 개발자 포털에서 발급받은 Bot Token 및 User ID (Intents: Message Content 활성화 필수)

## 설치 가이드 (Installation)

1. **레포지토리 클론 및 의존성 설치**
   ```bash
   git clone https://github.com/seohuda/discord-agy-remote.git
   cd discord-agy-remote
   pip install -r requirements.txt
   ```

2. **환경변수 설정**
   루트 디렉토리의 `.env.example` 파일을 복사하여 `.env` 파일을 생성한 뒤, 발급받은 Discord 토큰과 사용자의 고유 디스코드 ID를 입력합니다.
   ```bash
   cp .env.example .env
   # 이후 .env 파일 편집
   ```

3. **Shell Alias 등록 (Native UX 설정)**
   제공되는 설치 스크립트를 실행하여 사용 중인 Shell 설정 파일(`~/.bashrc` 또는 `~/.zshrc`)에 `agy` Alias를 자동 등록합니다. 이 과정을 통해 래퍼 스크립트를 `agy` 명령어로 투명하게 호출할 수 있습니다.
   ```bash
   chmod +x install.sh
   ./install.sh
   source ~/.bashrc  # 또는 사용하는 쉘에 맞춰 source ~/.zshrc 적용
   ```

## 사용법 (Usage)

1. **세션 시작**
   터미널에서 기존처럼 `agy` 명령어를 실행하여 Antigravity CLI 세션을 시작합니다. 필요시 추가 인자를 함께 넘길 수 있습니다 (예: `agy --help`).
   ```bash
   agy
   ```

2. **원격 제어 세션 활성화**
   현재 구동 중인 `agy` 세션 내에서 다음 명령어를 입력하면 디스코드 봇 클라이언트가 백그라운드에서 동적으로 할당되며 연동이 시작됩니다.
   ```text
   /rc on
   ```

3. **디스코드 원격 제어**
   봇 계정의 디스코드 DM으로 지시사항이나 프롬프트를 전송하면 실시간으로 로컬 CLI 세션에 전달되며, 에이전트의 작업 로그와 결과가 DM으로 회신됩니다. CLI와 디스코드 양쪽에서 동시 조작이 가능합니다.

4. **원격 제어 세션 종료**
   연동을 중단하고 싶은 경우, 로컬 CLI 입력창에 다음 명령어를 입력하여 디스코드 봇 인스턴스를 안전하게 종료하고 리소스를 반환합니다. 
   ```text
   /rc off
   ```
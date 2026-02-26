# OpenClaw 사용 가이드

OpenClaw는 AI 기반 코딩 어시스턴트로, Telegram, Discord, WhatsApp 등 다양한 채널을 통해 사용할 수 있습니다.

---

## 주요 기능

### 1. AI 코딩 어시스턴트
- 코드 작성, 리팩토링, 디버깅
- 파일 읽기/쓰기, 검색
- Git 작업 자동화
- 터미널 명령 실행
- 문서 작성 및 분석

### 2. 다중 채널 지원
- Telegram Bot
- Discord Bot
- WhatsApp (Linked Devices)
- Slack
- Chrome Extension

### 3. 확장 가능한 스킬 시스템
- 커스텀 스킬 추가
- 웹 검색 (Brave Search)
- 브라우저 자동화
- API 통합

---

## 기본 명령어

### 버전 및 상태 확인

```bash
# 버전 확인
openclaw --version

# 전체 상태 확인
openclaw health --verbose

# 도움말
openclaw --help
```

---

## 채널 관리

### 채널 목록 및 상태

```bash
# 설정된 채널 목록
openclaw channels list

# 채널 상태 확인
openclaw channels status

# 상세 상태 (프로브 포함)
openclaw channels status --probe

# 채널 로그 확인
openclaw channels logs
```

### Telegram 설정

```bash
# Telegram Bot 추가
openclaw channels add --channel telegram --token <BOT_TOKEN>

# DM 정책 설정
openclaw config set channels.telegram.dmPolicy "pairing"

# 허용 사용자 설정
openclaw config set channels.telegram.allowFrom '["@username"]'

# 스트리밍 모드
openclaw config set channels.telegram.streamMode "partial"
```

### WhatsApp 설정

```bash
# WhatsApp 로그인 (QR 코드 스캔)
openclaw channels login --channel whatsapp

# 허용 번호 설정
openclaw config set channels.whatsapp.allowFrom '["+821012345678"]'

# 로그아웃
openclaw channels logout --channel whatsapp
```

### Discord 설정

```bash
# Discord Bot 추가
openclaw channels add --channel discord --token <BOT_TOKEN>
```

---

## 설정 관리

### 설정 확인 및 변경

```bash
# 설정 확인
openclaw config get <path>

# 예시: Gateway 설정 확인
openclaw config get gateway

# 설정 변경
openclaw config set <path> <value>

# 예시: 모델 변경
openclaw config set agents.defaults.model.primary "amazon-bedrock/us.anthropic.claude-sonnet-4-20250514-v1:0"

# 설정 삭제
openclaw config unset <path>

# 대화형 설정 마법사
openclaw config
```

### 주요 설정 경로

```bash
# Gateway 설정
openclaw config get gateway.port
openclaw config get gateway.bind
openclaw config get gateway.auth.token

# 모델 설정
openclaw config get models.providers.amazon-bedrock
openclaw config get agents.defaults.model.primary

# 워크스페이스
openclaw config get agents.defaults.workspace

# Compaction (메모리 관리)
openclaw config get agents.defaults.compaction
```

---

## 에이전트 관리

### 에이전트 작업

```bash
# 에이전트 목록
openclaw agents list

# 새 에이전트 생성
openclaw agents create <name>

# 에이전트로 작업 실행
openclaw agent "코드 리뷰해줘"

# 특정 에이전트 사용
openclaw agent --agent <name> "작업 내용"
```

---

## 스킬 관리

### 스킬 확인

```bash
# 사용 가능한 스킬 목록
openclaw skills list

# 스킬 상태 확인 (요구사항 체크)
openclaw skills check

# 특정 스킬 정보
openclaw skills info <skill-name>
```

### 스킬 추가 (npm 패키지)

```bash
# humanizer 스킬 추가
npx skills add blader/humanizer --yes
ln -sf ~/.agents/skills/humanizer ~/clawd/skills/humanizer

# superpowers 스킬 추가 (14개 개발 스킬)
npx skills add obra/superpowers --yes
for skill in ~/.agents/skills/*/; do
  name=$(basename "$skill")
  ln -sf "$skill" ~/clawd/skills/"$name"
done

# 스킬 확인
openclaw skills list
```

---

## 브라우저 자동화

```bash
# 브라우저 상태
openclaw browser status

# 브라우저 시작
openclaw browser start

# 브라우저 중지
openclaw browser stop
```

---

## Dashboard & UI

### Dashboard (Web UI)

OpenClaw는 웹 기반 Control UI를 제공합니다.

**Private Subnet에서 접근 (SSM Port Forwarding):**

```bash
# 1. SSM Port Forwarding 설정
aws ssm start-session \
  --target <INSTANCE_ID> \
  --document-name AWS-StartPortForwardingSession \
  --parameters '{"portNumber":["18789"],"localPortNumber":["18789"]}' \
  --region us-west-2

# 2. 브라우저에서 접속
# http://localhost:18789/__openclaw__/canvas/

# 3. 또는 로컬에서 자동 열기
export OPENCLAW_GATEWAY_TOKEN="<YOUR_TOKEN>"
openclaw dashboard
```

**토큰 확인:**
```bash
# EC2에서 토큰 확인
cat ~/openclaw-token.txt

# 또는 설정에서 확인
openclaw config get gateway.auth.token
```

### TUI (Terminal UI)

터미널 기반 인터페이스:

```bash
# SSM으로 접속
aws ssm start-session --target <INSTANCE_ID> --region us-west-2

# ec2-user로 전환
sudo su - ec2-user

# TUI 실행
openclaw tui
```

### Doctor (진단 도구)

```bash
# 전체 시스템 진단 및 자동 수정
openclaw doctor

# Gateway 및 채널 상태 확인
openclaw doctor --verbose
```

---

## Gateway 관리

### Gateway 실행

```bash
# Gateway 시작 (포그라운드)
openclaw gateway run --bind loopback --port 18789 --token <TOKEN>

# systemd 서비스로 실행 (백그라운드)
sudo systemctl start openclaw-gateway.service
sudo systemctl stop openclaw-gateway.service
sudo systemctl restart openclaw-gateway.service
sudo systemctl status openclaw-gateway.service
```

### Gateway 로그

```bash
# systemd 로그 확인
sudo journalctl -u openclaw-gateway.service -f

# 최근 로그
sudo journalctl -u openclaw-gateway.service -n 100

# 에러만 확인
sudo journalctl -u openclaw-gateway.service | grep -i error
```

---

## SSM을 통한 원격 관리

Private Subnet EC2에서 OpenClaw를 관리하는 방법:

### 명령 실행

```bash
# 기본 명령 실행
aws ssm send-command \
  --document-name "AWS-RunShellScript" \
  --instance-ids "<INSTANCE_ID>" \
  --parameters 'commands=["sudo -u ec2-user openclaw health --verbose"]' \
  --region us-west-2

# 결과 확인
aws ssm get-command-invocation \
  --command-id "<COMMAND_ID>" \
  --instance-id "<INSTANCE_ID>" \
  --region us-west-2
```

### 주요 관리 작업

```bash
# Health Check
aws ssm send-command \
  --document-name "AWS-RunShellScript" \
  --instance-ids "<INSTANCE_ID>" \
  --parameters 'commands=["sudo -u ec2-user openclaw health --verbose"]' \
  --region us-west-2

# 채널 상태 확인
aws ssm send-command \
  --document-name "AWS-RunShellScript" \
  --instance-ids "<INSTANCE_ID>" \
  --parameters 'commands=["sudo -u ec2-user openclaw channels status"]' \
  --region us-west-2

# 서비스 재시작
aws ssm send-command \
  --document-name "AWS-RunShellScript" \
  --instance-ids "<INSTANCE_ID>" \
  --parameters 'commands=["systemctl restart openclaw-gateway.service","sleep 3","systemctl status openclaw-gateway.service"]' \
  --region us-west-2

# 설정 변경
aws ssm send-command \
  --document-name "AWS-RunShellScript" \
  --instance-ids "<INSTANCE_ID>" \
  --parameters 'commands=["sudo -u ec2-user openclaw config set <path> <value>","systemctl restart openclaw-gateway.service"]' \
  --region us-west-2
```

---

## 실제 사용 예시

### 1. Telegram Bot으로 코드 작성

1. Telegram에서 Bot에게 메시지 전송:
   ```
   Python으로 CSV 파일을 읽어서 JSON으로 변환하는 코드 작성해줘
   ```

2. AI가 코드 작성 및 파일 생성

3. 추가 요청:
   ```
   에러 처리 추가해줘
   ```

### 2. 파일 수정

```
~/clawd/app.py 파일의 main 함수를 리팩토링해줘
```

### 3. Git 작업

```
변경사항을 커밋하고 푸시해줘. 커밋 메시지는 "Add CSV to JSON converter"
```

### 4. 인프라 관리

```
AWS EC2 인스턴스 목록을 보여줘
```

### 5. 데이터 분석

```
data.csv 파일을 분석하고 요약 통계를 보여줘
```

---

## 트러블슈팅

### Gateway 연결 실패

```bash
# 서비스 상태 확인
systemctl status openclaw-gateway.service

# 포트 확인
ss -ltnp | grep 18789

# 토큰 확인
cat ~/openclaw-token.txt
openclaw config get gateway.auth.token

# 토큰 재설정
TOKEN=$(cat ~/openclaw-token.txt)
openclaw config set gateway.auth.token "$TOKEN"
systemctl restart openclaw-gateway.service
```

### 채널 연결 문제

```bash
# 채널 상태 확인
openclaw channels status --probe

# 채널 로그 확인
openclaw channels logs

# 채널 재연결
systemctl restart openclaw-gateway.service
```

### Bedrock 연결 문제

```bash
# AWS 권한 확인
aws sts get-caller-identity

# Bedrock 모델 목록 확인
aws bedrock list-foundation-models --region us-west-2

# VPC Endpoint 확인
aws ec2 describe-vpc-endpoints --region us-west-2
```

---

## 유용한 팁

### 1. 세션 리셋
Telegram에서 대화가 너무 길어지면:
```
/new
```

### 2. 컨텍스트 관리
Compaction 설정으로 자동 메모리 관리:
```bash
openclaw config set agents.defaults.compaction.reserveTokensFloor 20000
openclaw config set agents.defaults.compaction.memoryFlush.enabled true
```

### 3. 웹 검색 활성화
```bash
openclaw config set tools.web.search.provider "brave"
openclaw config set tools.web.search.apiKey "YOUR_API_KEY"
openclaw config set tools.web.fetch.enabled true
```

### 4. 로그 레벨 조정
```bash
openclaw --log-level debug health --verbose
```

---

## 참고 링크

- 공식 문서: https://docs.openclaw.ai
- GitHub: https://github.com/openclaw/openclaw
- CLI 문서: https://docs.openclaw.ai/cli
- 채널 설정: https://docs.openclaw.ai/cli/channels
- 스킬 문서: https://docs.openclaw.ai/cli/skills

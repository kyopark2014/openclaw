# OpenClaw Telegram Bot 설정 가이드

## 현재 설정

```yaml
Bot Username: @<YOUR_BOT_USERNAME>
Bot Token: <YOUR_BOT_TOKEN>
DM Policy: open
Allow From: * (모든 사용자)
Stream Mode: partial
```

## 사용 방법

1. Telegram에서 Bot username 검색
2. `/start` 명령 입력
3. 메시지를 보내면 AI가 응답

## 설정 변경

### SSM으로 설정 변경

```bash
# Bot Token 변경
aws ssm send-command \
  --document-name "AWS-RunShellScript" \
  --instance-ids <INSTANCE_ID> \
  --parameters 'commands=["sudo -u ec2-user openclaw config set channels.telegram.botToken \"NEW_TOKEN\"","systemctl restart openclaw-gateway.service"]' \
  --region us-west-2

# DM Policy 변경 (pairing 모드)
aws ssm send-command \
  --document-name "AWS-RunShellScript" \
  --instance-ids <INSTANCE_ID> \
  --parameters 'commands=["sudo -u ec2-user openclaw config set channels.telegram.dmPolicy pairing","sudo -u ec2-user openclaw config set channels.telegram.allowFrom \"[]\"","systemctl restart openclaw-gateway.service"]' \
  --region us-west-2
```

### 직접 SSH로 설정 (SSM Session)

```bash
# SSM Session 시작
aws ssm start-session --target <INSTANCE_ID> --region us-west-2

# 설정 변경
sudo -u ec2-user openclaw config set channels.telegram.botToken "YOUR_TOKEN"
sudo -u ec2-user openclaw config set channels.telegram.dmPolicy "open"
sudo -u ec2-user openclaw config set channels.telegram.allowFrom '["*"]'
sudo -u ec2-user openclaw config set channels.telegram.streamMode "partial"

# 서비스 재시작
sudo systemctl restart openclaw-gateway.service

# 상태 확인
sudo systemctl status openclaw-gateway.service
sudo journalctl -u openclaw-gateway.service -n 20 | grep telegram
```

## DM Policy 옵션

### open
- 누구나 Bot에 메시지 전송 가능
- `allowFrom`은 `["*"]`로 설정 필요

### pairing
- 사용자가 `/pair` 명령으로 pairing 요청
- 관리자가 승인해야 사용 가능
- `allowFrom`은 빈 배열 `[]`

### allowlist
- 특정 사용자만 허용
- `allowFrom`에 Telegram username 지정
- 예: `["@username1", "@username2"]`

## Stream Mode 옵션

- `partial`: 부분 스트리밍 (권장)
- `full`: 전체 스트리밍
- `off`: 스트리밍 없음 (완료 후 전체 응답)

## 트러블슈팅

### Bot이 응답하지 않음

```bash
# 로그 확인
aws ssm send-command \
  --document-name "AWS-RunShellScript" \
  --instance-ids <INSTANCE_ID> \
  --parameters 'commands=["journalctl -u openclaw-gateway.service -n 50 | grep -i telegram"]' \
  --region us-west-2

# 설정 확인
aws ssm send-command \
  --document-name "AWS-RunShellScript" \
  --instance-ids <INSTANCE_ID> \
  --parameters 'commands=["sudo -u ec2-user openclaw config get channels.telegram"]' \
  --region us-west-2
```

### "Unauthorized" 에러

Bot Token이 잘못되었거나 만료됨. BotFather에서 새 Token 발급:

```bash
# BotFather에서 /token 명령으로 새 Token 발급
# 새 Token으로 업데이트
aws ssm send-command \
  --document-name "AWS-RunShellScript" \
  --instance-ids <INSTANCE_ID> \
  --parameters 'commands=["sudo -u ec2-user openclaw config set channels.telegram.botToken \"NEW_TOKEN\"","systemctl restart openclaw-gateway.service"]' \
  --region us-west-2
```

### Pairing 승인

```bash
# Pending 요청 확인
aws ssm send-command \
  --document-name "AWS-RunShellScript" \
  --instance-ids <INSTANCE_ID> \
  --parameters 'commands=["sudo -u ec2-user openclaw devices list"]' \
  --region us-west-2

# 승인
aws ssm send-command \
  --document-name "AWS-RunShellScript" \
  --instance-ids <INSTANCE_ID> \
  --parameters 'commands=["sudo -u ec2-user openclaw devices approve <REQUEST_ID>"]' \
  --region us-west-2
```

## 새 Bot 생성

1. Telegram에서 [@BotFather](https://t.me/BotFather) 검색
2. `/newbot` 명령 입력
3. Bot 이름 입력 (예: OpenClaw Assistant)
4. Bot username 입력 (예: openclaw_assistant_bot)
   - 반드시 `_bot`으로 끝나야 함
5. BotFather가 제공하는 Token 복사
6. 위 설정 명령으로 Token 업데이트

## 보안 권장사항

### 프로덕션 환경

```bash
# allowlist 모드 사용
sudo -u ec2-user openclaw config set channels.telegram.dmPolicy allowlist
sudo -u ec2-user openclaw config set channels.telegram.allowFrom '["@your_username"]'

# 또는 pairing 모드
sudo -u ec2-user openclaw config set channels.telegram.dmPolicy pairing
sudo -u ec2-user openclaw config set channels.telegram.allowFrom '[]'
```

### 개발/테스트 환경

```bash
# open 모드 (현재 설정)
sudo -u ec2-user openclaw config set channels.telegram.dmPolicy open
sudo -u ec2-user openclaw config set channels.telegram.allowFrom '["*"]'
```

## 참고

- OpenClaw 문서: https://docs.openclaw.ai
- Telegram Bot API: https://core.telegram.org/bots/api

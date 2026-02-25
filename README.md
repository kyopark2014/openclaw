# OpenClaw


## Install

[Quick setup (CLI)](https://docs.openclaw.ai/start/getting-started)에 따라 아래와 같이 설치합니다.

```text
curl -fsSL https://openclaw.ai/install.sh | bash
```

## AWS 환경에서 OpenClaw에서 Bedrock 모델 설정하는 법

~/.openclaw/openclaw.json에서 두 가지 설정을 합니다.

### 모델 프로바이더 등록 (models.providers)

```java
"models": {
  "providers": {
    "amazon-bedrock": {
      "baseUrl": "https://bedrock-runtime.{region}.amazonaws.com",
      "auth": "aws-sdk",
      "api": "bedrock-converse-stream",
      "models": [
        {
          "id": "global.anthropic.claude-sonnet-4-6",
          "name": "Claude Sonnet 4.6",
          "reasoning": true,
          "input": ["text", "image"],
          "contextWindow": 200000,
          "maxTokens": 8192
        }
      ]
    }
  }
}
```

### 기본 모델 지정 (agents.defaults.model)

```java
"agents": {
  "defaults": {
    "model": {
      "primary": "amazon-bedrock/global.anthropic.claude-opus-4-6-v1",
      "fallbacks": ["amazon-bedrock/global.anthropic.claude-sonnet-4-5-20250929-v1:0"]
    }
  }
}
```

핵심 포인트는 아래와 같습니다.

- auth: "aws-sdk" → AWS credentials 자동 사용 (환경변수, ~/.aws/credentials, IAM role 등)
- 모델 ID는 {provider}/{model-id} 형식
- Cross-region inference 쓰려면 모델 ID에 global. prefix
- reasoning: true 설정하면 extended thinking 지원



• fallbacks로 primary 실패 시 자동 전환

전제조건: EC2에 Bedrock 접근 가능한 IAM role 붙이거나, AWS credentials 설정되어 있어야 함



## 사용방법

EC2에 SSM으로 접속후 아래와 같이 실행합니다.

```text
sudo su - ec2-user
openclaw --version
openclaw config get gateway
```

### 기본 명령어

- 버전 확인

```text
sudo -u ec2-user openclaw --version
```

- 설정 확인

```text
sudo -u ec2-user openclaw config get gateway
```


- 채널 상태 확인

```text
sudo -u ec2-user openclaw channels status
```

- Health 체크

```text
sudo -u ec2-user openclaw health --verbose
```

## Remote 설정

WahtsApp의 경우에 ALB에서 http로 연결하면서 secure 설정 문제로 접속이 불가합니다. Telegram은 쉽게 접속이 가능합니다.

## Telegram Bot 생성

1. Telegram에서 [@BotFather](https://t.me/BotFather)와 대화 시작
2. /newbot 명령 입력
3. Bot 이름 입력 (예: OpenClaw Assistant)
4. Bot username 입력 (예: openclaw_assistant_bot)
5. BotFather가 제공하는 Token을 복사

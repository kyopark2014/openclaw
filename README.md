# OpenClaw

전체적인 Architecture는 아래와 같습니다.

<img width="900" alt="image" src="https://github.com/user-attachments/assets/3e24f638-ab75-460c-a63f-bf0edca69d15" />

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

### Telegram Token

1. Telegram에서 [@BotFather](https://t.me/BotFather)와 대화 시작
2. /newbot 명령 입력
3. Bot 이름 입력 (예: OpenClaw Assistant)

### Slack

# Slack 설정 방법

Slack Bot을 설정하려면 App Token과 Bot Token이 필요합니다.

#### 1. Slack App 생성

1. https://api.slack.com/apps 접속
2. "Create New App" 클릭
3. "From scratch" 선택
4. App 이름 및 Workspace 선택

#### 2. Bot Token 발급

1. "OAuth & Permissions" 메뉴
2. "Bot Token Scopes" 추가:
   - chat:write
   - channels:history
   - groups:history
   - im:history
   - mpim:history
3. "Install to Workspace" 클릭
4. Bot User OAuth Token 복사 (xoxb-로 시작)

#### 3. App Token 발급

1. "Basic Information" 메뉴
2. "App-Level Tokens" 섹션
3. "Generate Token and Scopes" 클릭
4. Scope 추가: connections:write
5. App Token 복사 (xapp-로 시작)

#### 4. OpenClaw 설정
4. Bot username 입력 (예: openclaw_assistant_bot)
5. BotFather가 제공하는 Token을 복사



## Kiro-Cli 설치

아래와 같이 설치합니다.

```text
curl -fsSL https://cli.kiro.dev/install | bash
```

SSM으로 접속시 ec2-user로 전환합니다.

```text
sudo su - ec2-user
```

아래 방식으로 인증을 할 수 있습니다.

```text
$ kiro-cli login --use-device-flow
✔ Select login method · Use for Free with Builder ID

Confirm the following code in the browser
Code: VNCC-PKNS

Open this URL: https://view.awsapps.com/start/#/device?user_code=VNCC-PKNS
Device authorized
Logged in successfully
```

아래와 같이 실행합니다. 모델 설정은 claude-opus-4.6, claude-sonnet-4.6, claude-opus-4.5, claude-sonnet-4.5, claude-sonnet-4, claude-haiku-4.5, deepseek-3.2, minimax-m2.1, qwen3-coder-next 와 같이 선택할 수 있습니다.

```python
kiro-cli chat --model claude-sonnet-4.6
```


## Gateway restart

EC2에서 아래 명령어로 gateway를 재시작하세요:

```text
sudo systemctl restart openclaw-gateway.service
```

상태 확인은 아래와 같이 수행합니다.

```text
sudo systemctl status openclaw-gateway.service
```

로그 확인이 필요할때에는 아래와 같이 수행합니다.

```text
sudo journalctl -u openclaw-gateway.service -f
```

## 설치

### 설치하기

아래와 같이 [installer.py](./installer.py)로 설치를 시작합니다.

```text
python installer.py
```

아래와 같은 정보를 확인합니다.

<img width="500" alt="image" src="https://github.com/user-attachments/assets/d172dd55-df27-4660-b263-6e3f43a99910" />

이때, 아래와 같이 Telegram의 token을 생성한 후에 입력합니다.

<img width="600" src="https://github.com/user-attachments/assets/a84125fe-1d2f-4e53-b7a8-d5143bbd78b9" />

설치가 완료되면 아래와 같은 화면이 보입니다.

<img width="500" src="https://github.com/user-attachments/assets/2eb8c672-75b5-4e64-b397-8238d1d607c6" />

CloudFront의 주소로 접속한 후에 [Overview] - [Gateway Access] - [Gateway Token]에서 아래와 같이 gateway token을 입력합니다.

<img width="600" src="https://github.com/user-attachments/assets/20385a1e-0695-4a35-bbd3-2fd4dbde5998" />

Device를 등록하기 위해 SSM으로 접속합니다. 이때 아래와 같이 ec2-user로 전환합니다.

```text
sudo su - ec2-user
```

openclaw의 device 리스트를 확인합니다.

```text
openclaw devices list
```

이때의 결과는 아래와 같습니다.

<img width="900" alt="noname" src="https://github.com/user-attachments/assets/f95424a7-0221-4513-9523-5183b2221288" />

아래와 같이 접속한 디바이스에 권한을 부여합니다.

```text
openclaw devices approve [Device ID]
```

이때 request의 device id를 활용합니다.

<img width="600" alt="noname" src="https://github.com/user-attachments/assets/a2c2945c-339a-4064-b721-e46110f1fe45" />


이후 [Chat]에서 아래와 같이 OpenClaw의 Agent와 대화를 할 수 있습니다.

<img width="800" alt="image" src="https://github.com/user-attachments/assets/84f62e92-0da9-4c1d-9e3a-3838532672c0" />


# OpenClaw on AWS - 배포 가이드

Private Subnet EC2에 OpenClaw를 배포하고 Amazon Bedrock Claude를 연동하는 전체 과정입니다.

---

## 아키텍처 개요

```
                     ┌──────────────────┐
                     │   CloudFront     │
                     │   (HTTPS CDN)    │
                     │   <CF_DOMAIN>  │
                     └────────┬─────────┘
                              │ HTTP
┌─────────────────────────────┼───────────────────────────────────┐
│  VPC (10.25.0.0/16)         │                                   │
│                              │                                   │
│  ┌──────────────────────────┼───┐  ┌──────────────────────────┐│
│  │ Public Subnet (x2, 2AZ)  │   │  │ Private Subnet (x2)      ││
│  │                           │   │  │                          ││
│  │  ┌────────────────────────▼┐  │  │  ┌────────────────────┐ ││
│  │  │ ALB (Internet-facing)   │  │  │  │ EC2 Instance       │ ││
│  │  │ HTTP:80 → Target Group  │──┤──┤─▶│ OpenClaw Gateway   │ ││
│  │  └─────────────────────────┘  │  │  │ Port 18789         │ ││
│  │                               │  │  └──────┬─────────────┘ ││
│  │  ┌─────────────────────────┐  │  │         │               ││
│  │  │ NAT Gateway             │  │  │  ┌──────▼─────────────┐ ││
│  │  │ (아웃바운드 인터넷)      │◄─┤──┤  │ VPC Endpoint       │ ││
│  │  └─────────────────────────┘  │  │  │ Bedrock Runtime    │ ││
│  │                               │  │  │ (PrivateLink)      │ ││
│  │  ┌─────────────────────────┐  │  │  └────────────────────┘ ││
│  │  │ Internet Gateway        │  │  │                          ││
│  │  │ (인바운드/아웃바운드)    │  │  │                          ││
│  │  └─────────────────────────┘  │  │                          ││
│  └───────────────────────────────┘  └──────────────────────────┘│
└─────────────────────────────────────────────────────────────────┘
         │                                       │
         ▼                                       ▼
   ┌───────────┐                         ┌──────────────┐
   │ Internet  │                         │ AWS Bedrock   │
   │ Telegram  │                         │ Claude Sonnet │
   │ WhatsApp  │                         │ (us-west-2)   │
   └───────────┘                         └──────────────┘
```

**핵심 설계:**
- **CloudFront → ALB → EC2** 3계층 아키텍처
- CloudFront가 HTTPS 종단, CDN 캐싱, DDoS 보호(AWS Shield Standard) 제공
- ALB가 Public Subnet에서 로드 밸런싱 및 Health Check 수행
- EC2가 Private Subnet에 위치하여 인터넷에서 직접 접근 불가
- NAT Gateway를 통해 아웃바운드만 허용 (npm install, Telegram polling 등)
- Bedrock API는 VPC Endpoint(PrivateLink)로 AWS 내부 네트워크만 사용
- Telegram Bot은 polling 방식이라 인바운드 포트 오픈 불필요

---

## 현재 배포 정보

```yaml
Region: us-west-2
Account: <AWS_ACCOUNT_ID>
VPC: <VPC_ID> (10.25.0.0/16)
Public Subnets:
  - <PUBLIC_SUBNET_1>
  - <PUBLIC_SUBNET_2>
Private Subnets:
  - <PRIVATE_SUBNET_1>
  - <PRIVATE_SUBNET_2>
EC2 Instance: <INSTANCE_ID> (<PRIVATE_IP>)
ALB: <ALB_DNS>
CloudFront: <CF_DISTRIBUTION_ID> (<CF_DOMAIN>)
Bedrock VPC Endpoint: <VPCE_ID>
NAT Gateway: <NAT_GW_ID>
IAM Role: openclaw-ec2-bedrock-role
접속 URL: https://<CF_DOMAIN>/
Telegram Bot: @<YOUR_BOT_USERNAME>
```

---

## 사전 준비

```yaml
필요한 것:
  - AWS 계정 (Bedrock 사용 가능 리전: us-west-2 권장)
  - VPC + Public/Private Subnet (기존 또는 신규 생성)
  - Telegram 계정 (Bot 생성용)
  - SSH 키페어 (.pem 파일) - SSM 접속 시 불필요
```

---

## Phase 1: IAM Role 생성

EC2 인스턴스에 부여할 IAM Role을 생성합니다.

```bash
# 1. Trust Policy 생성
cat > /tmp/trust-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "Service": "ec2.amazonaws.com" },
      "Action": "sts:AssumeRole"
    }
  ]
}
EOF

# 2. IAM Role 생성
aws iam create-role \
  --role-name OpenClaw-Bedrock-Role \
  --assume-role-policy-document file:///tmp/trust-policy.json

# 3. Bedrock 권한 Policy 생성
cat > /tmp/bedrock-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream",
        "bedrock:ListFoundationModels"
      ],
      "Resource": "*"
    }
  ]
}
EOF

aws iam put-role-policy \
  --role-name OpenClaw-Bedrock-Role \
  --policy-name Bedrock-Access \
  --policy-document file:///tmp/bedrock-policy.json

# 4. Instance Profile 생성 및 연결
aws iam create-instance-profile \
  --instance-profile-name OpenClaw-Bedrock-Profile

aws iam add-role-to-instance-profile \
  --instance-profile-name OpenClaw-Bedrock-Profile \
  --role-name OpenClaw-Bedrock-Role
```

---

## Phase 2: Security Group 생성

```bash
# VPC ID 확인 (기존 VPC 사용 시)
VPC_ID="vpc-xxxxxxxxx"

# Security Group 생성
SG_ID=$(aws ec2 create-security-group \
  --group-name openclaw-sg \
  --description "OpenClaw Gateway Security Group" \
  --vpc-id $VPC_ID \
  --query 'GroupId' --output text)

echo "Security Group: $SG_ID"

# Inbound: SSH (관리용, 특정 IP만)
aws ec2 authorize-security-group-ingress \
  --group-id $SG_ID \
  --protocol tcp --port 22 \
  --cidr <YOUR_BASTION_IP>/32

# Outbound: HTTPS (Bedrock VPC Endpoint + 인터넷)
# 기본 아웃바운드 all 허용이면 별도 설정 불필요
# 제한하려면:
aws ec2 authorize-security-group-egress \
  --group-id $SG_ID \
  --protocol tcp --port 443 \
  --cidr 0.0.0.0/0
```

---

## Phase 3: VPC Endpoint 생성 (Bedrock Runtime)

Private Subnet에서 Bedrock API를 호출하기 위한 Interface VPC Endpoint를 생성합니다.

```bash
REGION="us-west-2"
SUBNET_IDS="subnet-xxxxxxxx,subnet-yyyyyyyy"  # Private Subnet IDs

# VPC Endpoint 생성
VPCE_ID=$(aws ec2 create-vpc-endpoint \
  --vpc-id $VPC_ID \
  --service-name com.amazonaws.${REGION}.bedrock-runtime \
  --vpc-endpoint-type Interface \
  --subnet-ids $SUBNET_IDS \
  --security-group-ids $SG_ID \
  --private-dns-enabled \
  --query 'VpcEndpoint.VpcEndpointId' --output text)

echo "VPC Endpoint: $VPCE_ID"
```

**주의:** Private DNS를 활성화하려면 VPC에서 DNS Hostnames가 활성화되어 있어야 합니다:
```bash
aws ec2 modify-vpc-attribute \
  --vpc-id $VPC_ID \
  --enable-dns-hostnames '{"Value": true}'
```

**Security Group에 VPC Endpoint 인바운드 추가:**
```bash
# Private Subnet CIDR에서 443 포트 허용
aws ec2 authorize-security-group-ingress \
  --group-id $SG_ID \
  --protocol tcp --port 443 \
  --cidr 10.0.11.0/24  # Private Subnet CIDR
```

---

## Phase 4: NAT Gateway 생성

Private Subnet에서 인터넷 접근이 필요합니다 (npm install, Telegram polling 등).

```bash
# 1. Elastic IP 할당
EIP_ALLOC=$(aws ec2 allocate-address \
  --domain vpc \
  --query 'AllocationId' --output text)

# 2. NAT Gateway 생성 (Public Subnet에)
NAT_ID=$(aws ec2 create-nat-gateway \
  --subnet-id <PUBLIC_SUBNET_ID> \
  --allocation-id $EIP_ALLOC \
  --query 'NatGateway.NatGatewayId' --output text)

echo "NAT Gateway: $NAT_ID"

# 3. Private Subnet Route Table에 NAT 경로 추가
aws ec2 create-route \
  --route-table-id <PRIVATE_RT_ID> \
  --destination-cidr-block 0.0.0.0/0 \
  --nat-gateway-id $NAT_ID
```

---

## Phase 5: EC2 인스턴스 생성

**중요:** Private Subnet에 생성하여 보안을 강화합니다.

```bash
INSTANCE_ID=$(aws ec2 run-instances \
  --image-id ami-075b5421f670d735c \
  --instance-type t3.medium \
  --key-name <YOUR_KEY_PAIR_NAME> \
  --subnet-id <PRIVATE_SUBNET_ID> \
  --security-group-ids $SG_ID \
  --iam-instance-profile Name=OpenClaw-Bedrock-Profile \
  --block-device-mappings '[{"DeviceName":"/dev/xvda","Ebs":{"VolumeSize":50,"VolumeType":"gp3"}}]' \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=openclaw-private}]' \
  --query 'Instances[0].InstanceId' --output text)

echo "Instance: $INSTANCE_ID"

# 인스턴스 시작 대기
aws ec2 wait instance-running --instance-ids $INSTANCE_ID
```

**인스턴스 타입 권장:**
```yaml
개인 사용: t3.medium (2 vCPU, 4GB) - 월 ~$30
팀 사용:   t3.xlarge (4 vCPU, 16GB) - 월 ~$120
```

**주의사항:**
- Private Subnet에 생성되므로 Public IP가 할당되지 않습니다
- 인터넷 접근은 NAT Gateway를 통해서만 가능합니다
- 관리는 AWS Systems Manager를 통해서만 가능합니다

---

## Phase 6: EC2 초기 설정

**중요:** Private Subnet에 있으므로 SSH 직접 접속이 불가능합니다. AWS Systems Manager Session Manager를 사용합니다.

### SSM Session Manager로 접속

```bash
# Session Manager 플러그인 설치 (최초 1회)
# macOS
curl "https://s3.amazonaws.com/session-manager-downloads/plugin/latest/mac/sessionmanager-bundle.zip" -o "sessionmanager-bundle.zip"
unzip sessionmanager-bundle.zip
sudo ./sessionmanager-bundle/install -i /usr/local/sessionmanagerplugin -b /usr/local/bin/session-manager-plugin

# 접속
aws ssm start-session --target <INSTANCE_ID> --region us-west-2
```

### 또는 SSM 명령 실행

```bash
# 명령 실행
aws ssm send-command \
  --document-name "AWS-RunShellScript" \
  --instance-ids <INSTANCE_ID> \
  --parameters 'commands=["node --version","openclaw --version"]' \
  --region us-west-2

# 결과 확인 (CommandId는 위 명령 결과에서 확인)
aws ssm get-command-invocation \
  --command-id <COMMAND_ID> \
  --instance-id <INSTANCE_ID> \
  --region us-west-2
```

### Node.js 설치

**중요:** Amazon Linux 2023은 기본적으로 Node.js 18을 설치하지만, OpenClaw는 Node.js 22가 필요합니다.

```bash
# 기존 Node.js 제거
sudo dnf remove -y nodejs nodejs-full-i18n npm

# Node.js 22 설치
curl -fsSL https://rpm.nodesource.com/setup_22.x | sudo bash -
sudo dnf install -y nodejs --allowerasing

# Node.js 22+ 확인
node --version  # v22.22.0 이상이어야 함
```

### 타임존 설정 (선택)

```bash
sudo timedatectl set-timezone Asia/Seoul
```

### AWS CLI 확인

```bash
aws sts get-caller-identity
# Expected: OpenClaw-Bedrock-Role

aws bedrock list-foundation-models --region us-west-2 \
  --query 'modelSummaries[?contains(modelId, `claude`)].modelId' \
  --output table
```

---

## Phase 7: OpenClaw 설치

```bash
sudo npm install -g openclaw@latest

# 버전 확인
openclaw --version
```

---

## Phase 8: OpenClaw 설정

### Gateway 기본 설정

```bash
openclaw config set gateway.mode local
openclaw config set gateway.port 18789
openclaw config set gateway.bind loopback
```

### Amazon Bedrock 모델 설정

```bash
# 모델 프로바이더 설정
openclaw config set models.providers.amazon-bedrock.baseUrl \
  "https://bedrock-runtime.us-west-2.amazonaws.com"
openclaw config set models.providers.amazon-bedrock.auth "aws-sdk"
openclaw config set models.providers.amazon-bedrock.api "bedrock-converse-stream"

# 모델 추가 (Claude Opus 4.6 예시)
openclaw config set models.providers.amazon-bedrock.models '[
  {
    "id": "global.anthropic.claude-opus-4-6-v1",
    "name": "Claude Opus 4.6 (Bedrock Global)",
    "reasoning": true,
    "input": ["text", "image"],
    "cost": { "input": 0.015, "output": 0.075 },
    "contextWindow": 200000,
    "maxTokens": 8192
  }
]'

# 기본 모델 지정
openclaw config set agents.defaults.model.primary \
  "amazon-bedrock/global.anthropic.claude-opus-4-6-v1"

# 워크스페이스 설정
openclaw config set agents.defaults.workspace ~/clawd
mkdir -p ~/clawd
```

**사용 가능한 Bedrock Claude 모델:**
```yaml
Claude Opus 4.6:
  ID: global.anthropic.claude-opus-4-6-v1
  Context: 200K tokens
  Cost: Input $0.015, Output $0.075 / 1K tokens

Claude Sonnet 4.5:
  ID: global.anthropic.claude-sonnet-4-5-20250929-v1:0
  Context: 200K tokens
  Cost: Input $0.003, Output $0.015 / 1K tokens
```

### Compaction 설정 (권장)

대화가 길어졌을 때 자동으로 컨텍스트를 요약합니다:

```bash
openclaw config set agents.defaults.compaction.reserveTokensFloor 20000
openclaw config set agents.defaults.compaction.memoryFlush.enabled true
openclaw config set agents.defaults.compaction.memoryFlush.softThresholdTokens 4000
```

### 설정 확인

```bash
cat ~/.openclaw/openclaw.json | python3 -m json.tool
```

---

## Phase 9: Telegram Bot 연동

### 1. BotFather에서 Bot 생성

Telegram에서 @BotFather 검색 후:
```
/newbot
→ Bot 이름 입력 (예: "My OpenClaw Bot")
→ Username 입력 (반드시 'bot'으로 끝남, 예: "my_openclaw_bot")
→ Bot Token 복사
```

### 2. OpenClaw에 Telegram 설정

```bash
# Bot Token 설정
openclaw config set channels.telegram.botToken "YOUR_BOT_TOKEN"

# DM 정책 (pairing = 첫 메시지에 페어링 코드 필요)
openclaw config set channels.telegram.dmPolicy "pairing"

# 허용 사용자 (Telegram username)
openclaw config set channels.telegram.allowFrom '["@your_username"]'

# 스트리밍 모드
openclaw config set channels.telegram.streamMode "partial"
```

### 3. Telegram 연결 확인

```bash
openclaw channels status
# Expected: Telegram default: enabled, configured, mode:polling
```

---

## Phase 10: systemd 서비스 설정

Gateway를 백그라운드 서비스로 실행합니다:

```bash
# Gateway Auth Token 생성
TOKEN=$(openssl rand -hex 32)
echo "Auth Token: $TOKEN"

# 토큰을 OpenClaw 설정에 추가
openclaw config set gateway.auth.token "$TOKEN"

# systemd 서비스 파일 생성
sudo tee /etc/systemd/system/openclaw-gateway.service > /dev/null << EOF
[Unit]
Description=OpenClaw Gateway Service
After=network.target

[Service]
Type=simple
User=ec2-user
WorkingDirectory=/home/ec2-user
Environment="PATH=/usr/bin:/usr/local/bin"
Environment="NODE_ENV=production"
Environment="AWS_REGION=us-west-2"
Environment="OPENCLAW_GATEWAY_TOKEN=${TOKEN}"
ExecStart=/usr/bin/openclaw gateway run --bind loopback --port 18789 --token ${TOKEN}
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=openclaw-gateway

[Install]
WantedBy=multi-user.target
EOF

# 서비스 활성화 및 시작
sudo systemctl daemon-reload
sudo systemctl enable openclaw-gateway.service
sudo systemctl start openclaw-gateway.service

# 상태 확인
sudo systemctl status openclaw-gateway.service
```

---

## Phase 11: ALB (Application Load Balancer) 설정

CloudFront의 Origin으로 사용할 ALB를 생성합니다. Public Subnet에 배치하여 CloudFront → ALB → EC2 경로를 구성합니다.

### 1. ALB용 Public Subnet 확인 (최소 2개, 다른 AZ 필요)

```bash
VPC_ID="<VPC_ID>"

# 현재 배포: 2개 Public Subnet
# - <PUBLIC_SUBNET_1> (us-west-2a)
# - <PUBLIC_SUBNET_2> (us-west-2b)
```

### 2. ALB Security Group 생성

```bash
ALB_SG=$(aws ec2 create-security-group \
  --group-name openclaw-ec2-alb-sg \
  --description "OpenClaw ALB Security Group" \
  --vpc-id $VPC_ID \
  --query 'GroupId' --output text)

# HTTP 인바운드 허용 (CloudFront에서 HTTP로 연결)
aws ec2 authorize-security-group-ingress \
  --group-id $ALB_SG \
  --protocol tcp --port 80 --cidr 0.0.0.0/0
```

### 3. EC2 Security Group에 ALB 허용

```bash
EC2_SG="<EC2_SG_ID>"

# ALB에서 EC2:18789로의 트래픽 허용
aws ec2 authorize-security-group-ingress \
  --group-id $EC2_SG \
  --protocol tcp --port 18789 \
  --source-group $ALB_SG
```

### 4. ALB 생성

```bash
ALB_ARN=$(aws elbv2 create-load-balancer \
  --name alb-openclaw-ec2 \
  --subnets <PUBLIC_SUBNET_1> <PUBLIC_SUBNET_2> \
  --security-groups $ALB_SG \
  --scheme internet-facing \
  --type application \
  --query 'LoadBalancers[0].LoadBalancerArn' --output text)

# 현재 배포: <ALB_DNS>
```

### 5. Target Group 생성

```bash
TG_ARN=$(aws elbv2 create-target-group \
  --name tg-openclaw-ec2 \
  --protocol HTTP \
  --port 18789 \
  --vpc-id $VPC_ID \
  --health-check-path / \
  --health-check-protocol HTTP \
  --matcher "HttpCode=200-401" \
  --query 'TargetGroups[0].TargetGroupArn' --output text)

# EC2 인스턴스 등록
aws elbv2 register-targets \
  --target-group-arn $TG_ARN \
  --targets Id=<INSTANCE_ID>,Port=18789
```

### 6. Listener 생성

```bash
aws elbv2 create-listener \
  --load-balancer-arn $ALB_ARN \
  --protocol HTTP \
  --port 80 \
  --default-actions Type=forward,TargetGroupArn=$TG_ARN
```

### 7. Gateway 설정 변경 (lan bind)

```bash
aws ssm send-command \
  --document-name "AWS-RunShellScript" \
  --instance-ids <INSTANCE_ID> \
  --parameters 'commands=["sudo -u ec2-user openclaw config set gateway.bind lan","sudo -u ec2-user openclaw config set gateway.controlUi.dangerouslyAllowHostHeaderOriginFallback true","sudo -u ec2-user openclaw config set gateway.trustedProxies \"[\\\"10.25.0.0/16\\\"]\"","systemctl restart openclaw-gateway.service"]' \
  --region us-west-2
```

### 8. ALB 접속 확인

```bash
# ALB DNS로 직접 접속 (HTTP)
http://<ALB_DNS>/

# Target Health 확인
aws elbv2 describe-target-health \
  --target-group-arn <TG_ARN>
```

---

## Phase 12: CloudFront Distribution 생성

ALB 앞에 CloudFront를 배치하여 HTTPS, CDN 캐싱, DDoS 보호를 제공합니다.

### 1. CloudFront Distribution 생성

```bash
# ALB를 Origin으로 하는 CloudFront 배포 생성
CALLER_REF="openclaw-ec2-$(date +%s)"
ALB_DNS="<ALB_DNS>"

aws cloudfront create-distribution \
  --distribution-config '{
    "CallerReference": "'$CALLER_REF'",
    "Comment": "openclaw-ec2 CloudFront",
    "Enabled": true,
    "HttpVersion": "http2",
    "PriceClass": "PriceClass_All",
    "Origins": {
      "Quantity": 1,
      "Items": [{
        "Id": "openclaw-alb-origin",
        "DomainName": "'$ALB_DNS'",
        "CustomOriginConfig": {
          "HTTPPort": 80,
          "HTTPSPort": 443,
          "OriginProtocolPolicy": "http-only",
          "OriginSslProtocols": {"Quantity": 1, "Items": ["TLSv1.2"]}
        }
      }]
    },
    "DefaultCacheBehavior": {
      "TargetOriginId": "openclaw-alb-origin",
      "ViewerProtocolPolicy": "redirect-to-https",
      "AllowedMethods": {
        "Quantity": 7,
        "Items": ["GET","HEAD","OPTIONS","PUT","POST","PATCH","DELETE"],
        "CachedMethods": {"Quantity": 2, "Items": ["GET","HEAD"]}
      },
      "Compress": true,
      "ForwardedValues": {
        "QueryString": true,
        "Cookies": {"Forward": "all"},
        "Headers": {"Quantity": 1, "Items": ["*"]}
      },
      "MinTTL": 0,
      "DefaultTTL": 0,
      "MaxTTL": 0
    },
    "ViewerCertificate": {"CloudFrontDefaultCertificate": true}
  }'

# 현재 배포: <CF_DISTRIBUTION_ID> (<CF_DOMAIN>)
```

### 2. 배포 상태 확인

CloudFront 배포는 전 세계 엣지 로케이션에 전파하는 데 10-15분이 소요됩니다.

```bash
# 배포 상태 확인
aws cloudfront get-distribution \
  --id <CF_DISTRIBUTION_ID> \
  --query 'Distribution.Status' \
  --output text

# "Deployed" 상태가 되면 접속 가능
```

### 3. Gateway에 CloudFront Origin 허용 설정

```bash
aws ssm send-command \
  --document-name "AWS-RunShellScript" \
  --instance-ids <INSTANCE_ID> \
  --parameters 'commands=["sudo -u ec2-user openclaw config set gateway.controlUi.allowedOrigins \"[\\\"https://<CF_DOMAIN>\\\",\\\"http://<ALB_DNS>\\\"]\"","systemctl restart openclaw-gateway.service"]' \
  --region us-west-2
```

### 4. CloudFront 접속 확인

```bash
# HTTPS로 접속 (권장)
https://<CF_DOMAIN>/

# 토큰 입력 화면이 나타나면 성공
```

### CloudFront 설정 요약

```yaml
Origin:
  Protocol: HTTP only (ALB → CloudFront는 HTTP)
  Port: 80

Viewer:
  Protocol: HTTPS redirect (클라이언트 → CloudFront는 HTTPS)
  Allowed Methods: GET, HEAD, OPTIONS, PUT, POST, PATCH, DELETE

Cache:
  TTL: 0 (API 트래픽이므로 캐싱 비활성)
  Query String: 모두 포워딩
  Cookies: 모두 포워딩
  Headers: 모두 포워딩

보안:
  - AWS Shield Standard 자동 적용 (DDoS 보호)
  - TLS 1.2 지원
  - IPv6 활성화
```

### CloudFront 캐시 무효화

```bash
# 전체 캐시 무효화
aws cloudfront create-invalidation \
  --distribution-id <CF_DISTRIBUTION_ID> \
  --paths "/*"
```

---

## Phase 13: 최종 검증

```bash
# 1. CloudFront HTTPS 접속 확인
curl -I https://<CF_DOMAIN>/

# 2. ALB HTTP 접속 확인
curl -I http://<ALB_DNS>/

# 3. Gateway Health Check (SSM으로)
aws ssm send-command \
  --document-name "AWS-RunShellScript" \
  --instance-ids <INSTANCE_ID> \
  --parameters 'commands=["openclaw health --verbose","ss -ltnp | grep 18789","systemctl status openclaw-gateway.service"]' \
  --region us-west-2

# 4. Bedrock 연결 테스트
aws bedrock-runtime converse \
  --model-id "us.anthropic.claude-sonnet-4-20250514-v1:0" \
  --messages '[{"role":"user","content":[{"text":"Say OK"}]}]' \
  --region us-west-2

# 5. Telegram에서 @<YOUR_BOT_USERNAME> 에 메시지 전송
#    "안녕하세요!" → AI 응답 확인
```

---

## 보안 Best Practices

### 네트워크 격리
```yaml
CloudFront: HTTPS 종단, AWS Shield Standard (DDoS 보호)
ALB: Public Subnet, HTTP:80 (CloudFront에서만 접근)
EC2: Private Subnet (Public IP 없음), ALB에서만 18789 접근 허용
아웃바운드: NAT Gateway 경유 (HTTPS만)
Bedrock: VPC Endpoint (AWS 내부 네트워크, 인터넷 미사용)
```

### 인증
```yaml
Gateway: Token 기반 인증 (lan 바인딩, trustedProxies로 VPC CIDR 제한)
CloudFront: Viewer → HTTPS redirect, Origin → HTTP only
Telegram: allowFrom으로 허용 사용자 제한
IAM Role: Bedrock 최소 권한만 부여
```

### 데이터 보호
```yaml
메모리 파일: EC2 로컬 파일시스템 (~/.openclaw/, ~/clawd/)
세션 로그: ~/.openclaw/agents/main/sessions/*.jsonl
크레덴셜: ~/.openclaw/credentials/ (파일 권한 600)
전송 암호화: CloudFront TLS 1.2 (클라이언트 ↔ CloudFront)
```

### 프로덕션 강화 (선택)
```yaml
WAF: CloudFront에 Web ACL 연결하여 악성 요청 차단
Custom Header: CloudFront → ALB 간 시크릿 헤더로 직접 ALB 접속 차단
IP 제한: ALB SG에서 CloudFront IP 범위만 허용
인증서: ACM 커스텀 도메인 + HTTPS
```

---

## 트래픽 흐름

### 웹 대시보드 접속 (CloudFront 경유)
```
Browser (HTTPS)
  → CloudFront (<CF_DOMAIN>)
    → ALB (<ALB_DNS>, HTTP:80)
      → EC2 (<PRIVATE_IP>:18789, Private Subnet)
```

### Telegram 메시지 수신 (Polling)
```
EC2 → NAT Gateway → Internet → Telegram Server
  "새 메시지 있나요?" (Long Polling)
  ← "사용자 메시지 도착"
```

### Bedrock API 호출
```
EC2 → VPC Endpoint → AWS PrivateLink → Bedrock Runtime
  (인터넷 미사용, AWS 백본 네트워크만)
```

### Telegram 응답 전송
```
EC2 → NAT Gateway → Internet → Telegram Server
  "사용자에게 응답 전달"
```

### WhatsApp 연동 (웹 Dashboard 경유)
```
Browser → CloudFront (HTTPS) → ALB → EC2 → QR 코드 스캔
EC2 → NAT Gateway → Internet → WhatsApp Server (Linked Devices)
```

---

## 월간 예상 비용

| 항목 | 사양 | 월간 비용 |
|------|------|----------|
| EC2 | t3.medium | ~$30 |
| EBS | 50GB gp3 | ~$4.40 |
| NAT Gateway | 기본 | ~$33 |
| VPC Endpoint | 2 AZ | ~$15 |
| ALB | internet-facing | ~$16 |
| CloudFront | 월 10GB 기준 | ~$1.50 |
| **인프라 소계** | | **~$100/월** |
| Bedrock (Sonnet 4) | 사용량 기반 | 변동 |
| Bedrock (Opus 4.6) | 사용량 기반 | 변동 |

**Bedrock 토큰 가격:**
```yaml
Claude Sonnet 4:   Input $0.003, Output $0.015 / 1K tokens
Claude Opus 4.6:   Input $0.015, Output $0.075 / 1K tokens
```

**CloudFront 비용 상세:**
```yaml
데이터 전송 (아웃): $0.085/GB (미국/유럽), $0.14/GB (아시아)
HTTP/HTTPS 요청: $0.0075/10,000 요청
무료 티어: 월 1TB 데이터 전송 + 10M 요청 (12개월)
```

---

## 선택 사항

### WhatsApp 연동

```bash
openclaw config set channels.whatsapp.dmPolicy "allowlist"
openclaw config set channels.whatsapp.allowFrom '["+821012345678"]'

# QR 코드 스캔 (Linked Devices)
openclaw channels login --channel whatsapp
```

### Chrome Extension 연결

Private Subnet이라 직접 연결 불가. SSM Port Forwarding 사용:

```bash
# SSM Port Forwarding
aws ssm start-session \
  --target <INSTANCE_ID> \
  --document-name AWS-StartPortForwardingSession \
  --parameters '{"portNumber":["18789"],"localPortNumber":["18789"]}' \
  --region us-west-2

# Chrome Extension 설정
# Gateway URL: ws://localhost:18789
# Auth Token: <GATEWAY_TOKEN>
```

### 웹 검색 기능 (Brave Search)

```bash
# Brave Search API 키 발급: https://brave.com/search/api/
openclaw config set tools.web.search.provider "brave"
openclaw config set tools.web.search.apiKey "YOUR_BRAVE_API_KEY"
openclaw config set tools.web.fetch.enabled true
```

### 스킬 추가

```bash
# humanizer (AI 문체 자연스럽게)
npx skills add blader/humanizer --yes
ln -sf ~/.agents/skills/humanizer ~/clawd/skills/humanizer

# superpowers (14개 개발 스킬)
npx skills add obra/superpowers --yes
for skill in ~/.agents/skills/*/; do
  name=$(basename "$skill")
  ln -sf "$skill" ~/clawd/skills/"$name"
done

# 확인
openclaw skills list
```

---

## 유지보수

### 업데이트

```bash
# SSM으로 명령 실행
aws ssm send-command \
  --document-name "AWS-RunShellScript" \
  --instance-ids <INSTANCE_ID> \
  --parameters 'commands=["sudo npm install -g openclaw@latest","sudo systemctl restart openclaw-gateway.service","openclaw --version"]' \
  --region us-west-2
```

### 로그 확인

```bash
# SSM으로 로그 확인
aws ssm send-command \
  --document-name "AWS-RunShellScript" \
  --instance-ids <INSTANCE_ID> \
  --parameters 'commands=["sudo journalctl -u openclaw-gateway.service -n 50"]' \
  --region us-west-2

# 결과 확인
aws ssm get-command-invocation \
  --command-id <COMMAND_ID> \
  --instance-id <INSTANCE_ID> \
  --region us-west-2
```

### CloudFront 캐시 무효화

업데이트 후 CloudFront 캐시를 무효화하여 최신 콘텐츠를 전달합니다:

```bash
aws cloudfront create-invalidation \
  --distribution-id <CF_DISTRIBUTION_ID> \
  --paths "/*"
```

## 트러블슈팅

### "Input is too long" 에러
```bash
# Telegram에서 /new 입력 (세션 리셋)
# 또는 SSM으로 compaction 설정 확인
aws ssm send-command \
  --document-name "AWS-RunShellScript" \
  --instance-ids <INSTANCE_ID> \
  --parameters 'commands=["openclaw config get agents.defaults.compaction"]' \
  --region us-west-2
```

### 플러그인 로드 실패 (EACCES)
```bash
aws ssm send-command \
  --document-name "AWS-RunShellScript" \
  --instance-ids <INSTANCE_ID> \
  --parameters 'commands=["sudo chown -R ec2-user:ec2-user /tmp/jiti/","sudo systemctl restart openclaw-gateway.service"]' \
  --region us-west-2
```

### Gateway 토큰 문제
```bash
# 토큰이 설정되지 않은 경우
aws ssm send-command \
  --document-name "AWS-RunShellScript" \
  --instance-ids <INSTANCE_ID> \
  --parameters 'commands=["TOKEN=$(cat /home/ec2-user/openclaw-token.txt)","sudo -u ec2-user openclaw config set gateway.auth.token \"$TOKEN\"","systemctl restart openclaw-gateway.service"]' \
  --region us-west-2

# 토큰 확인
aws ssm send-command \
  --document-name "AWS-RunShellScript" \
  --instance-ids <INSTANCE_ID> \
  --parameters 'commands=["cat /home/ec2-user/openclaw-token.txt","sudo -u ec2-user openclaw config get gateway.auth"]' \
  --region us-west-2
```

### Node.js 버전 문제
```bash
# Node.js 버전 확인 및 재설치
aws ssm send-command \
  --document-name "AWS-RunShellScript" \
  --instance-ids <INSTANCE_ID> \
  --parameters 'commands=["node --version","npm --version"]' \
  --region us-west-2

# Node.js 22가 아니면 재설치
aws ssm send-command \
  --document-name "AWS-RunShellScript" \
  --instance-ids <INSTANCE_ID> \
  --parameters 'commands=["sudo dnf remove -y nodejs","curl -fsSL https://rpm.nodesource.com/setup_22.x | sudo bash -","sudo dnf install -y nodejs --allowerasing","node --version"]' \
  --region us-west-2
```

### Gateway Bind 설정 문제 (ALB 외부 접속)
```bash
# bind를 lan으로 변경 (0.0.0.0 리스닝)
aws ssm send-command \
  --document-name "AWS-RunShellScript" \
  --instance-ids <INSTANCE_ID> \
  --parameters 'commands=["sudo -u ec2-user openclaw config set gateway.bind lan","sudo -u ec2-user openclaw config set gateway.controlUi.dangerouslyAllowHostHeaderOriginFallback true","systemctl restart openclaw-gateway.service"]' \
  --region us-west-2

# 설정 확인
aws ssm send-command \
  --document-name "AWS-RunShellScript" \
  --instance-ids <INSTANCE_ID> \
  --parameters 'commands=["sudo -u ec2-user openclaw config get gateway","ss -ltnp | grep 18789"]' \
  --region us-west-2
```

**중요**: 
- `bind: "loopback"` - localhost만 접속 가능 (기본값)
- `bind: "lan"` - 0.0.0.0으로 리스닝, ALB/외부 접속 가능
- ALB 사용 시 `dangerouslyAllowHostHeaderOriginFallback: true` 필요
- `trustedProxies: ["10.25.0.0/16"]` - ALB Proxy 헤더 신뢰
- 접속 URL: `https://<CF_DOMAIN>/` (CloudFront HTTPS)

### CloudFront 502 Bad Gateway
```bash
# ALB Target Health 확인
aws elbv2 describe-target-health \
  --target-group-arn <TG_ARN>

# Gateway 실행 상태 확인
aws ssm send-command \
  --document-name "AWS-RunShellScript" \
  --instance-ids <INSTANCE_ID> \
  --parameters 'commands=["systemctl status openclaw-gateway.service","ss -ltnp | grep 18789","curl -I http://localhost:18789/"]' \
  --region us-west-2
```

### CloudFront 403 Forbidden
CloudFront 배포 진행 중이면 403이 발생합니다. 10-15분 대기 후 재시도하세요.
```bash
aws cloudfront get-distribution \
  --id <CF_DISTRIBUTION_ID> \
  --query 'Distribution.Status' \
  --output text
# "Deployed" 상태 확인
```

---

## Phase 14: Telegram Bot 설정

Telegram Bot을 통해 OpenClaw AI와 대화할 수 있습니다.

### 1. Bot 생성

1. Telegram에서 [@BotFather](https://t.me/BotFather) 검색
2. `/newbot` 명령 입력
3. Bot 이름 및 username 설정
4. Token 복사

### 2. Bot 설정

```bash
# SSM으로 설정
aws ssm send-command \
  --document-name "AWS-RunShellScript" \
  --instance-ids <INSTANCE_ID> \
  --parameters 'commands=["sudo -u ec2-user openclaw config set channels.telegram.enabled true","sudo -u ec2-user openclaw config set channels.telegram.botToken \"YOUR_BOT_TOKEN\"","sudo -u ec2-user openclaw config set channels.telegram.dmPolicy open","sudo -u ec2-user openclaw config set channels.telegram.allowFrom \"[\\\"*\\\"]\"","sudo -u ec2-user openclaw config set channels.telegram.streamMode partial","systemctl restart openclaw-gateway.service"]' \
  --region us-west-2
```

### 3. 사용

1. Telegram에서 Bot 검색
2. `/start` 명령 입력
3. 메시지 전송

**현재 Bot**: @<YOUR_BOT_USERNAME>

자세한 내용은 `telegram-setup.md` 참조

---

## 참고 링크

- OpenClaw 공식: https://openclaw.ai
- OpenClaw GitHub: https://github.com/openclaw/openclaw
- OpenClaw 문서: https://docs.openclaw.ai
- Amazon Bedrock: https://docs.aws.amazon.com/bedrock/
- CloudFront 문서: https://docs.aws.amazon.com/cloudfront/
- CloudFront 가격: https://aws.amazon.com/cloudfront/pricing/
- VPC Endpoint 가이드: https://docs.aws.amazon.com/vpc/latest/privatelink/
- AWS Systems Manager: https://docs.aws.amazon.com/systems-manager/

---

## 빠른 시작 (자동화 스크립트)

전체 과정을 자동화한 `installer.py` 스크립트를 사용할 수 있습니다:

```bash
# 아키텍처: CloudFront → ALB → EC2 (Private Subnet) + Telegram
# VPC/Subnet/IGW/NAT/SG/VPC Endpoint/IAM/EC2/ALB/CloudFront 한 번에 생성

python installer.py --telegram-bot-token "YOUR_BOT_TOKEN"

# 옵션:
#   --region us-west-2 (기본)
#   --instance-type t3.medium (기본)
#   --disable-cloudfront (CloudFront 비활성)
#   --telegram-dm-policy open|pairing|allowlist
#   --key-name <KEY_PAIR_NAME> (SSM 접속 시 불필요)

# 삭제 (모든 리소스 정리):
python uninstaller.py
```

자세한 내용은 `ec2-userdata.sh`, `deployment-info.md`, `telegram-setup.md`, `cloudfront-setup.md`, `alb-setup.md` 파일을 참조하세요.

## 주요 문서

- `deployment-info.md` - 생성된 리소스 정보 및 관리 명령어
- `telegram-setup.md` - Telegram Bot 설정 가이드
- `cloudfront-setup.md` - CloudFront 배포 가이드
- `alb-setup.md` - ALB 외부 접속 설정
- `use_command.md` - OpenClaw 사용 가이드


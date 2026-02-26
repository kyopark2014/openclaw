#!/bin/bash
set -euo pipefail

# OpenClaw EC2 User Data Script
# Amazon Linux 2023 + Node.js 22 + OpenClaw + Bedrock
# 아키텍처: CloudFront → ALB → EC2 (Private Subnet)

# --- 사용자 설정 (installer.py 사용 시 자동 주입됨) ---
VPC_CIDR="${VPC_CIDR:-10.25.0.0/16}"
TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
AWS_REGION="${AWS_REGION:-us-west-2}"

exec > >(tee /var/log/openclaw-install.log)
exec 2>&1

echo "=== OpenClaw 설치 시작 ==="

dnf remove -y nodejs nodejs-full-i18n npm || true
curl -fsSL https://rpm.nodesource.com/setup_22.x | bash -
dnf install -y nodejs git --allowerasing

timedatectl set-timezone Asia/Seoul || true

npm install -g openclaw@latest

mkdir -p /home/ec2-user/.openclaw /home/ec2-user/clawd

TOKEN=$(openssl rand -hex 32)
echo "$TOKEN" > /home/ec2-user/openclaw-token.txt

cat > /home/ec2-user/.openclaw/openclaw.json << ENDCONFIG
{
  "gateway": {
    "port": 18789,
    "mode": "local",
    "bind": "lan",
    "auth": {
      "token": "${TOKEN}"
    },
    "controlUi": {
      "dangerouslyAllowHostHeaderOriginFallback": true
    },
    "trustedProxies": [
      "${VPC_CIDR}"
    ]
  },
  "models": {
    "providers": {
      "amazon-bedrock": {
        "baseUrl": "https://bedrock-runtime.${AWS_REGION}.amazonaws.com",
        "auth": "aws-sdk",
        "api": "bedrock-converse-stream",
        "models": [
          {
            "id": "us.anthropic.claude-sonnet-4-20250514-v1:0",
            "name": "Claude Sonnet 4",
            "reasoning": true,
            "input": ["text", "image"],
            "cost": {
              "input": 0.003,
              "output": 0.015
            },
            "contextWindow": 200000,
            "maxTokens": 8192
          },
          {
            "id": "us.anthropic.claude-opus-4-20250514-v1:0",
            "name": "Claude Opus 4",
            "reasoning": true,
            "input": ["text", "image"],
            "cost": {
              "input": 0.015,
              "output": 0.075
            },
            "contextWindow": 200000,
            "maxTokens": 8192
          }
        ]
      }
    }
  },
  "agents": {
    "defaults": {
      "model": {
        "primary": "amazon-bedrock/us.anthropic.claude-sonnet-4-20250514-v1:0"
      },
      "workspace": "/home/ec2-user/clawd",
      "compaction": {
        "reserveTokensFloor": 20000,
        "memoryFlush": {
          "enabled": true,
          "softThresholdTokens": 4000
        }
      }
    }
  },
  "channels": {
    "telegram": {
      "enabled": true,
      "botToken": "${TELEGRAM_BOT_TOKEN}",
      "dmPolicy": "open",
      "allowFrom": ["*"],
      "streamMode": "partial"
    }
  }
}
ENDCONFIG

chown -R ec2-user:ec2-user /home/ec2-user/.openclaw /home/ec2-user/clawd /home/ec2-user/openclaw-token.txt

cat > /etc/systemd/system/openclaw-gateway.service << 'SVC'
[Unit]
Description=OpenClaw Gateway Service
After=network.target

[Service]
Type=simple
User=ec2-user
WorkingDirectory=/home/ec2-user
Environment="PATH=/usr/bin:/usr/local/bin"
Environment="NODE_ENV=production"
SVC

cat >> /etc/systemd/system/openclaw-gateway.service << SVCENV
Environment="AWS_REGION=${AWS_REGION}"
ExecStart=/usr/bin/env openclaw gateway run --bind lan --port 18789 --token ${TOKEN}
SVCENV

cat >> /etc/systemd/system/openclaw-gateway.service << 'SVC'
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=openclaw-gateway

[Install]
WantedBy=multi-user.target
SVC

systemctl daemon-reload
systemctl enable openclaw-gateway.service
systemctl start openclaw-gateway.service

echo "=== OpenClaw 설치 완료 ==="
echo "Gateway Token: $TOKEN"
echo "Telegram Bot Token: ${TELEGRAM_BOT_TOKEN:-'Not set - configure manually'}"

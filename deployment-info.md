# OpenClaw AWS 배포 리소스 정보

## 생성 시각 (UTC)
- 2026-02-26T12:35:57.782024+00:00

## 핵심 리소스

| 항목 | 값 |
|------|-----|
| Region | us-west-2 |
| Account | 262976740991 |
| VPC | vpc-03153963b9a91e8c0 (10.25.0.0/16) |
| Public Subnets | subnet-02e97a262330d73a8, subnet-0ba6281bcb9a3b3bf |
| Private Subnets | subnet-0139ef8b330b6e888, subnet-0d7cb74cbea322428 |
| EC2 Instance | i-0870b74ecb6ddd124 (10.25.11.134) |
| EC2 SG | sg-0a1c7018f92d66977 |
| ALB | alb-openclaw-546982726.us-west-2.elb.amazonaws.com |
| ALB SG | sg-0c79b19cf66053254 |
| Target Group | arn:aws:elasticloadbalancing:us-west-2:262976740991:targetgroup/tg-openclaw/1cf3efb16800c850 |
| Bedrock VPC Endpoint | vpce-03a198160878e57de |
| NAT Gateway | nat-01b58a29959b058dd |
| IAM Role | OpenClaw-Bedrock-Role |
| Instance Profile | openclaw-bedrock-profile |
| CloudFront | E3QKG8DNMHEG82 (d3u6aurginhbnw.cloudfront.net) |

## 접속 URL
- CloudFront (HTTPS): https://d3u6aurginhbnw.cloudfront.net/
- ALB (HTTP): http://alb-openclaw-546982726.us-west-2.elb.amazonaws.com/

## Gateway Token
```
b6f04e6c2b7dec62a146c66cc1ab8c9a9fd47c731ae50f87af1ba7f4bb86d40b
```

## Telegram
- dmPolicy: open
- allowFrom: ["*"]
- streamMode: partial

## SSM 접속
```bash
aws ssm start-session --target i-0870b74ecb6ddd124 --region us-west-2
```

## 설치 로그 확인
```bash
aws ssm send-command \
  --document-name "AWS-RunShellScript" \
  --instance-ids "i-0870b74ecb6ddd124" \
  --parameters 'commands=["tail -100 /var/log/openclaw-install.log"]' \
  --region us-west-2
```

## 서비스 관리
```bash
sudo systemctl start   openclaw-gateway.service
sudo systemctl stop    openclaw-gateway.service
sudo systemctl restart openclaw-gateway.service
sudo systemctl status  openclaw-gateway.service
sudo journalctl -u openclaw-gateway.service -f
```

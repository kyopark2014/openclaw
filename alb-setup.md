# OpenClaw ALB 외부 접속 설정 가이드

WhatsApp 연동을 위해 외부에서 OpenClaw Dashboard에 접속하는 방법입니다.

## 아키텍처

```
Internet → ALB (Public Subnet) → EC2:18789 (Private Subnet)
```

## 현재 배포 정보

```yaml
VPC: <VPC_ID>
Public Subnets:
  - <PUBLIC_SUBNET_1> (us-west-2a, 10.100.1.0/24)
  - <PUBLIC_SUBNET_2> (us-west-2b, 10.100.2.0/24)
Private Subnet:
  - <PRIVATE_SUBNET_1> (us-west-2a, 10.100.11.0/24)
EC2 Instance: <INSTANCE_ID>
EC2 Private IP: <PRIVATE_IP>
ALB: <ALB_DNS>
Target Group: openclaw-tg (port 18789)
```

## 접속 URL

```
http://<ALB_DNS>/
```

**중요**: 
- 루트 경로(`/`)로 접속해야 합니다
- `/__openclaw__/canvas/`는 API 엔드포인트라 401 에러 발생
- 브라우저에서 접속하면 토큰 입력 화면이 나타남

## Gateway 토큰

```bash
# SSM으로 토큰 확인
aws ssm send-command \
  --document-name "AWS-RunShellScript" \
  --instance-ids <INSTANCE_ID> \
  --parameters 'commands=["cat /home/ec2-user/openclaw-token.txt"]' \
  --region us-west-2
```

현재 토큰: `<GATEWAY_TOKEN>`

## Gateway 설정 (중요)

ALB를 통한 외부 접속을 위해서는 다음 설정이 필요합니다:

```json
{
  "gateway": {
    "bind": "lan",  // 0.0.0.0으로 리스닝
    "controlUi": {
      "dangerouslyAllowHostHeaderOriginFallback": true  // ALB Host 헤더 허용
    }
  }
}
```

### 설정 변경 방법

```bash
# SSM으로 설정 변경
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

## Bind 옵션 설명

| Bind 값 | 리스닝 주소 | 용도 |
|---------|------------|------|
| `loopback` | 127.0.0.1 | 로컬 접속만 (기본값) |
| `lan` | 0.0.0.0 | LAN/ALB 접속 가능 |
| `tailnet` | Tailscale | VPN 접속 |
| `auto` | 자동 감지 | - |
| `custom` | 사용자 지정 | - |

## 트러블슈팅

### 502 Bad Gateway

**원인**: Gateway가 Private IP로 리스닝하지 않음

**해결**:
```bash
# bind 설정 확인
aws ssm send-command \
  --document-name "AWS-RunShellScript" \
  --instance-ids <INSTANCE_ID> \
  --parameters 'commands=["sudo -u ec2-user openclaw config get gateway.bind"]' \
  --region us-west-2

# lan으로 변경
aws ssm send-command \
  --document-name "AWS-RunShellScript" \
  --instance-ids <INSTANCE_ID> \
  --parameters 'commands=["sudo -u ec2-user openclaw config set gateway.bind lan","systemctl restart openclaw-gateway.service"]' \
  --region us-west-2
```

### 401 Unauthorized (JSON 에러)

**원인**: 
1. Canvas 경로(`/__openclaw__/canvas/`)로 직접 접속
2. `dangerouslyAllowHostHeaderOriginFallback` 미설정

**해결**:
```bash
# 루트 경로로 접속
http://<ALB_DNS>/

# 설정 추가
aws ssm send-command \
  --document-name "AWS-RunShellScript" \
  --instance-ids <INSTANCE_ID> \
  --parameters 'commands=["sudo -u ec2-user openclaw config set gateway.controlUi.dangerouslyAllowHostHeaderOriginFallback true","systemctl restart openclaw-gateway.service"]' \
  --region us-west-2
```

### Target Unhealthy

**원인**: Health Check 실패

**확인**:
```bash
# Target Health 확인
aws elbv2 describe-target-health \
  --target-group-arn <TG_ARN> \
  --region us-west-2

# Gateway 상태 확인
aws ssm send-command \
  --document-name "AWS-RunShellScript" \
  --instance-ids <INSTANCE_ID> \
  --parameters 'commands=["systemctl status openclaw-gateway.service","ss -ltnp | grep 18789","curl -i http://localhost:18789/"]' \
  --region us-west-2
```

## WhatsApp 연동

1. 브라우저에서 ALB URL 접속
2. Gateway 토큰 입력
3. Dashboard에서 WhatsApp 메뉴 선택
4. QR 코드 스캔 (휴대폰: <YOUR_PHONE_NUMBER>)
5. 연동 완료

## 보안 고려사항

### 현재 설정 (개발/테스트용)
- HTTP만 사용 (암호화 없음)
- 인터넷 전체에 오픈 (0.0.0.0/0)
- Host Header Fallback 활성화

### 프로덕션 권장사항
1. **HTTPS 적용**
   - ACM 인증서 발급
   - ALB Listener를 HTTPS로 변경
   
2. **IP 제한**
   - ALB Security Group에서 특정 IP만 허용
   - WAF 적용

3. **인증 강화**
   - Cognito 연동
   - 토큰 주기적 갱신

4. **allowedOrigins 명시**
   ```bash
   openclaw config set gateway.controlUi.allowedOrigins '["http://<ALB_DNS>"]'
   ```

## 비용

- ALB: ~$16/월 (시간당 $0.0225)
- 데이터 전송: 사용량에 따라 변동
- 기존 NAT Gateway, EC2 비용은 동일

## 참고

- OpenClaw 문서: https://docs.openclaw.ai
- ALB 가이드: https://docs.aws.amazon.com/elasticloadbalancing/

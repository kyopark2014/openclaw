# OpenClaw CloudFront 배포 가이드

CloudFront → ALB → EC2 구조로 HTTPS 접속을 제공합니다.

## 배포 정보

```yaml
CloudFront:
  Distribution ID: <CF_DISTRIBUTION_ID>
  Domain: <CF_DOMAIN>
  Status: Deployed

Origin:
  Type: Custom (ALB)
  Domain: <ALB_DNS>
  Protocol: HTTP only

Cache:
  TTL: 0 (API 트래픽, 캐싱 비활성)
  Query String: 모두 포워딩
  Cookies: 모두 포워딩
  Headers: 모두 포워딩
```

## 아키텍처

```
Internet (HTTPS)
    ↓
CloudFront (<CF_DOMAIN>)
    ↓ HTTP
ALB (<ALB_DNS>)
    ↓ HTTP
EC2 Instance (<PRIVATE_IP>:18789)
    ↓
OpenClaw Gateway
```

## 접속 URL

### CloudFront (HTTPS - 권장)
```
https://<CF_DOMAIN>/
```

### ALB (HTTP)
```
http://<ALB_DNS>/
```

## Gateway 토큰

```bash
# SSM으로 토큰 확인
aws ssm send-command \
  --document-name "AWS-RunShellScript" \
  --instance-ids <INSTANCE_ID> \
  --parameters 'commands=["cat /home/ec2-user/openclaw-token.txt"]' \
  --region us-west-2
```

## 배포 상태 확인

```bash
aws cloudfront get-distribution \
  --id <CF_DISTRIBUTION_ID> \
  --query 'Distribution.Status' \
  --output text

# "Deployed" 상태가 되면 접속 가능 (신규 배포 시 10-15분 소요)
```

## Gateway 설정

CloudFront와 ALB 모두 허용하도록 설정되었습니다:

```json
{
  "gateway": {
    "bind": "lan",
    "trustedProxies": ["10.25.0.0/16"],
    "controlUi": {
      "allowedOrigins": [
        "http://<ALB_DNS>",
        "https://<CF_DOMAIN>"
      ],
      "dangerouslyAllowHostHeaderOriginFallback": true
    }
  }
}
```

## CloudFront 특징

### 장점
- **HTTPS 자동 제공**: CloudFront 기본 인증서 사용
- **글로벌 CDN**: 전 세계 엣지 로케이션에서 캐싱
- **DDoS 보호**: AWS Shield Standard 자동 적용
- **성능 향상**: 정적 콘텐츠 캐싱, gzip/brotli 압축

### 설정
- **Viewer Protocol**: HTTPS로 리다이렉트
- **Allowed Methods**: GET, HEAD, OPTIONS, PUT, POST, PATCH, DELETE
- **Compress**: 활성화 (gzip/brotli)
- **HTTP Version**: HTTP/2
- **IPv6**: 활성화
- **TTL**: 0 (API 트래픽이므로 캐싱 비활성)

## 비용

```yaml
CloudFront:
  - 데이터 전송 (아웃): $0.085/GB (첫 10TB, 미국/유럽)
  - 데이터 전송 (아웃): $0.14/GB (아시아)
  - HTTP/HTTPS 요청: $0.0075/10,000 요청
  - 무료 티어: 월 1TB 데이터 전송 + 10M 요청 (12개월)

예상 비용 (월 10GB 사용 시):
  - 데이터 전송: ~$1.40
  - 요청: ~$0.01
  - 합계: ~$1.50/월
```

## WhatsApp 연동

CloudFront URL을 사용하여 WhatsApp을 연동하세요:

1. 브라우저에서 접속:
   ```
   https://<CF_DOMAIN>/
   ```

2. Gateway 토큰 입력

3. WhatsApp 메뉴에서 QR 코드 스캔

## 트러블슈팅

### 502 Bad Gateway (CloudFront)

**원인**: ALB가 unhealthy 또는 Gateway 미실행

**확인**:
```bash
# ALB Target Health
aws elbv2 describe-target-health \
  --target-group-arn <TG_ARN>

# Gateway 상태
aws ssm send-command \
  --document-name "AWS-RunShellScript" \
  --instance-ids <INSTANCE_ID> \
  --parameters 'commands=["systemctl status openclaw-gateway.service","ss -ltnp | grep 18789","curl -I http://localhost:18789/"]' \
  --region us-west-2
```

### 403 Forbidden

**원인**: CloudFront 배포 중 또는 Origin 접근 불가

**해결**: 10-15분 대기 후 재시도

### WebSocket 연결 실패

**원인**: CloudFront는 WebSocket을 지원하지만 추가 설정 필요

**해결**: ALB URL을 직접 사용하거나 CloudFront에서 WebSocket 경로 별도 설정

## CloudFront 관리

### 캐시 무효화 (Invalidation)

```bash
# 전체 캐시 무효화
aws cloudfront create-invalidation \
  --distribution-id <CF_DISTRIBUTION_ID> \
  --paths "/*"

# 특정 경로만
aws cloudfront create-invalidation \
  --distribution-id <CF_DISTRIBUTION_ID> \
  --paths "/index.html" "/__openclaw__/*"
```

### 배포 비활성화

```bash
# 1. 현재 설정 가져오기
aws cloudfront get-distribution-config \
  --id <CF_DISTRIBUTION_ID> \
  --output json > /tmp/cf-config.json

# 2. Enabled를 false로 변경 (수동 편집)

# 3. 업데이트
aws cloudfront update-distribution \
  --id <CF_DISTRIBUTION_ID> \
  --if-match <ETAG> \
  --distribution-config file:///tmp/cf-config.json
```

### 배포 삭제

```bash
# 1. 비활성화 (위 단계)
# 2. Deployed 상태 대기
# 3. 삭제
aws cloudfront delete-distribution \
  --id <CF_DISTRIBUTION_ID> \
  --if-match <ETAG>

# 또는 uninstaller.py 사용 (모든 리소스 일괄 삭제)
python uninstaller.py
```

## 보안 강화 (선택사항)

### Custom Header 추가

ALB에서 특정 헤더가 있는 요청만 허용:

```bash
# CloudFront 설정에 Custom Header 추가
# Origin → Custom Headers:
#   X-Custom-Header: <random-secret-value>

# ALB에서 해당 헤더 검증 (WAF 또는 Lambda@Edge)
```

### WAF 연동

```bash
# Web ACL 생성 후 CloudFront에 연결
aws cloudfront update-distribution \
  --id <CF_DISTRIBUTION_ID> \
  --web-acl-id <WAF_WEB_ACL_ARN>
```

## 참고 링크

- CloudFront 문서: https://docs.aws.amazon.com/cloudfront/
- CloudFront 가격: https://aws.amazon.com/cloudfront/pricing/
- Cache Policy: https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/controlling-the-cache-key.html

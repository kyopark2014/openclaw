#!/bin/bash
# OpenClaw EC2 인스턴스 생성 스크립트 (수동)
# 전체 자동 배포는 installer.py 사용을 권장합니다.

set -euo pipefail

REGION="us-west-2"
VPC_ID="<VPC_ID>"
SUBNET_ID="<PRIVATE_SUBNET_1>"    # Private Subnet (us-west-2a)
SG_ID="<EC2_SG_ID>"            # openclaw-ec2-ec2-sg
INSTANCE_PROFILE="openclaw-ec2-bedrock-profile"
INSTANCE_TYPE="t3.medium"
AMI_ID="ami-075b5421f670d735c"          # Amazon Linux 2023 (us-west-2)
VOLUME_SIZE=50
PROJECT_NAME="openclaw-ec2"

USER_DATA_FILE="./ec2-userdata.sh"

if [ ! -f "$USER_DATA_FILE" ]; then
  echo "ERROR: $USER_DATA_FILE 파일이 없습니다."
  exit 1
fi

echo "=== OpenClaw EC2 인스턴스 생성 ==="
echo "  Region   : $REGION"
echo "  VPC      : $VPC_ID"
echo "  Subnet   : $SUBNET_ID (Private)"
echo "  SG       : $SG_ID"
echo "  Profile  : $INSTANCE_PROFILE"
echo "  Type     : $INSTANCE_TYPE"
echo "  AMI      : $AMI_ID"
echo "  Volume   : ${VOLUME_SIZE}GB gp3"
echo ""

INSTANCE_ID=$(aws ec2 run-instances \
  --image-id "$AMI_ID" \
  --instance-type "$INSTANCE_TYPE" \
  --subnet-id "$SUBNET_ID" \
  --security-group-ids "$SG_ID" \
  --iam-instance-profile "Name=$INSTANCE_PROFILE" \
  --block-device-mappings "[{\"DeviceName\":\"/dev/xvda\",\"Ebs\":{\"VolumeSize\":$VOLUME_SIZE,\"VolumeType\":\"gp3\"}}]" \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$PROJECT_NAME}]" \
  --user-data "file://$USER_DATA_FILE" \
  --region "$REGION" \
  --query 'Instances[0].InstanceId' \
  --output text)

echo "Instance ID: $INSTANCE_ID"
echo "인스턴스 시작 대기 중..."
aws ec2 wait instance-running --instance-ids "$INSTANCE_ID" --region "$REGION"

PRIVATE_IP=$(aws ec2 describe-instances \
  --instance-ids "$INSTANCE_ID" \
  --region "$REGION" \
  --query 'Reservations[0].Instances[0].PrivateIpAddress' \
  --output text)

echo ""
echo "=== 인스턴스 생성 완료 ==="
echo "  Instance ID : $INSTANCE_ID"
echo "  Private IP  : $PRIVATE_IP"
echo ""
echo "SSM 접속:"
echo "  aws ssm start-session --target $INSTANCE_ID --region $REGION"
echo ""
echo "설치 로그 확인:"
echo "  aws ssm send-command --document-name \"AWS-RunShellScript\" --instance-ids \"$INSTANCE_ID\" --parameters 'commands=[\"tail -50 /var/log/openclaw-install.log\"]' --region $REGION"
echo ""
echo "NOTE: ALB Target Group에 수동 등록이 필요합니다:"
echo "  aws elbv2 register-targets --target-group-arn <TG_ARN> --targets Id=$INSTANCE_ID,Port=18789 --region $REGION"

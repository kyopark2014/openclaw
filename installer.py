#!/usr/bin/env python3
"""
OpenClaw AWS one-shot installer.

VPC/Subnet/IGW/NAT/Route Table/SecurityGroup/VPC Endpoint/IAM Role/
EC2(UserData)/ALB/CloudFront 를 한 번에 자동으로 생성합니다.

아키텍처:  CloudFront -> ALB -> EC2 (OpenClaw Gateway + Telegram)
"""

from __future__ import annotations

import ipaddress
import json
import logging
import secrets
import sys
import textwrap
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import boto3
from botocore.exceptions import ClientError, NoCredentialsError

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
PROJECT_NAME = "openclaw"
REGION = "us-west-2"
AMI_ID = "ami-075b5421f670d735c"  # Amazon Linux 2023 (us-west-2)
INSTANCE_TYPE = "t3.medium"
GATEWAY_PORT = 18789
VOLUME_SIZE = 50
CONFIG_PATH = Path("openclaw-config.json")
DEPLOYMENT_INFO_PATH = Path("assets/deployment-info.md")
SKILLS_PATH = Path(__file__).resolve().parent / "skills"
CUSTOM_HEADER_NAME = "X-Origin-Verify"  # CloudFront → ALB 요청 검증용 (직접 ALB 접근 차단)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ===================================================================
# Utility helpers
# ===================================================================

def _safe_create(create_fn, already_code: str, *, label: str = ""):
    """Call *create_fn*; swallow if error code == *already_code*."""
    try:
        return create_fn()
    except ClientError as exc:
        if exc.response["Error"]["Code"] == already_code:
            logger.warning("  이미 존재: %s", label)
            return None
        raise


def _get_available_cidr(ec2) -> str:
    candidates = [
        "10.20.0.0/16", "10.21.0.0/16", "10.22.0.0/16",
        "10.23.0.0/16", "10.24.0.0/16", "10.25.0.0/16",
    ]
    existing = set()
    for vpc in ec2.describe_vpcs()["Vpcs"]:
        existing.add(vpc["CidrBlock"])
        for assoc in vpc.get("CidrBlockAssociationSet", []):
            existing.add(assoc["CidrBlock"])
    for cidr in candidates:
        if cidr not in existing:
            return cidr
    return "10.30.0.0/16"


def _wait_nat(ec2, nat_id: str) -> None:
    for _ in range(60):
        state = ec2.describe_nat_gateways(NatGatewayIds=[nat_id])[
            "NatGateways"
        ][0]["State"]
        if state == "available":
            return
        time.sleep(10)
    raise RuntimeError(f"NAT Gateway {nat_id} 가 available 되지 않았습니다.")


def _wait_subnet(ec2, subnet_id: str, timeout: int = 120) -> None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        state = ec2.describe_subnets(SubnetIds=[subnet_id])["Subnets"][0]["State"]
        if state == "available":
            return
        time.sleep(5)


def _classify_subnets(ec2, subnets: List[Dict]) -> Dict[str, List[str]]:
    pub, priv = [], []
    for s in subnets:
        name = ""
        for t in s.get("Tags", []):
            if t["Key"] == "Name":
                name = t["Value"]
        if "public" in name.lower():
            pub.append(s["SubnetId"])
        elif "private" in name.lower():
            priv.append(s["SubnetId"])
        else:
            try:
                rts = ec2.describe_route_tables(
                    Filters=[{"Name": "association.subnet-id", "Values": [s["SubnetId"]]}]
                )["RouteTables"]
                is_pub = any(
                    r.get("GatewayId", "").startswith("igw-")
                    for rt in rts for r in rt["Routes"]
                )
                (pub if is_pub else priv).append(s["SubnetId"])
            except Exception:
                priv.append(s["SubnetId"])
    return {"public": pub, "private": priv}


def _authorize_ingress(ec2, sg_id: str, perm: Dict) -> None:
    try:
        ec2.authorize_security_group_ingress(GroupId=sg_id, IpPermissions=[perm])
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "InvalidPermission.Duplicate":
            raise


def _get_or_create_sg(ec2, vpc_id: str, name: str, desc: str) -> str:
    resp = ec2.describe_security_groups(
        Filters=[
            {"Name": "group-name", "Values": [name]},
            {"Name": "vpc-id", "Values": [vpc_id]},
        ]
    )
    if resp["SecurityGroups"]:
        sg_id = resp["SecurityGroups"][0]["GroupId"]
        logger.info("  SG 재사용: %s (%s)", name, sg_id)
        return sg_id
    sg_id = ec2.create_security_group(
        GroupName=name, Description=desc, VpcId=vpc_id,
        TagSpecifications=[{
            "ResourceType": "security-group",
            "Tags": [{"Key": "Name", "Value": name}],
        }],
    )["GroupId"]
    logger.info("  SG 생성: %s (%s)", name, sg_id)
    return sg_id


# ===================================================================
# Main installer class
# ===================================================================

class Installer:
    def __init__(self, *, region: str = REGION, project: str = PROJECT_NAME):
        self.region = region
        self.project = project
        self.session = boto3.Session(region_name=region)
        self.ec2 = self.session.client("ec2")
        self.iam = self.session.client("iam")
        self.elbv2 = self.session.client("elbv2")
        self.cf = self.session.client("cloudfront")
        self.sts = self.session.client("sts")
        self.s3 = self.session.client("s3")
        self.opensearch = self.session.client("opensearchserverless")
        self.bedrock_agent = self.session.client("bedrock-agent")
        try:
            self.account_id = self.sts.get_caller_identity()["Account"]
        except NoCredentialsError:
            logger.error("AWS 자격 증명을 찾을 수 없습니다.")
            sys.exit(1)

        self.out: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Entrypoint
    # ------------------------------------------------------------------
    def run(
        self,
        telegram_bot_token: str,
        telegram_dm_policy: str = "open",
        telegram_allow_from: Optional[List[str]] = None,
        telegram_stream_mode: str = "partial",
        key_name: Optional[str] = None,
        instance_type: str = INSTANCE_TYPE,
        ami_id: str = AMI_ID,
        volume_size: int = VOLUME_SIZE,
        enable_cloudfront: bool = True,
        enable_knowledge_base: bool = True,
        config_path: Path = CONFIG_PATH,
        deployment_info_path: Path = DEPLOYMENT_INFO_PATH,
    ) -> Dict[str, Any]:
        if telegram_allow_from is None:
            telegram_allow_from = ["*"]

        kb_steps = 5 if enable_knowledge_base else 0  # S3, KB Role, OpenSearch, Index, KB
        if enable_knowledge_base and SKILLS_PATH.exists() and SKILLS_PATH.is_dir():
            kb_steps += 1  # Skills 업로드
        total = 10 + kb_steps if enable_cloudfront else 9 + kb_steps
        step = 0

        def _step(desc: str) -> None:
            nonlocal step
            step += 1
            logger.info("%d) %s  [%d/%d]", step, desc, step, total)

        _step("VPC / Subnet / IGW / NAT Gateway")
        net = self._ensure_vpc_networking()

        vpc_id = net["vpc_id"]
        vpc_cidr = net["vpc_cidr"]
        public_subnets = net["public_subnets"]
        private_subnets = net["private_subnets"]

        s3_bucket_name = None
        knowledge_base_role_arn = None
        opensearch_info = None
        knowledge_base_id = None
        skills_s3_uri = None

        # Skills 업로드: KB 활성화 시 또는 skills 폴더가 있으면 S3에 업로드
        needs_s3_for_skills = SKILLS_PATH.exists() and SKILLS_PATH.is_dir()
        if enable_knowledge_base:
            _step("S3 Bucket 생성 (Knowledge Base용)")
            s3_bucket_name = self._create_s3_bucket()
            if needs_s3_for_skills:
                _step("Skills 폴더 S3 업로드")
                self._upload_skills_to_s3(s3_bucket_name, SKILLS_PATH)
                skills_s3_uri = f"s3://{s3_bucket_name}/artifacts/skills/"
            _step("Knowledge Base IAM Role 생성")
            knowledge_base_role_arn = self._ensure_knowledge_base_role()
        elif needs_s3_for_skills:
            _step("S3 Bucket 생성 (Skills용)")
            s3_bucket_name = self._create_s3_bucket()
            _step("Skills 폴더 S3 업로드")
            self._upload_skills_to_s3(s3_bucket_name, SKILLS_PATH)
            skills_s3_uri = f"s3://{s3_bucket_name}/artifacts/skills/"

        _step("IAM Role + Instance Profile")
        role_name, profile_name, profile_arn = self._ensure_iam(
            knowledge_base_role_arn=knowledge_base_role_arn,
        )

        _step("Security Groups (EC2, ALB)")
        ec2_sg = _get_or_create_sg(self.ec2, vpc_id, f"{self.project}-ec2-sg", "OpenClaw EC2 SG")
        alb_sg = _get_or_create_sg(self.ec2, vpc_id, f"{self.project}-alb-sg", "OpenClaw ALB SG")
        _authorize_ingress(self.ec2, alb_sg, {
            "IpProtocol": "tcp", "FromPort": 80, "ToPort": 80,
            "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
        })
        _authorize_ingress(self.ec2, ec2_sg, {
            "IpProtocol": "tcp", "FromPort": GATEWAY_PORT, "ToPort": GATEWAY_PORT,
            "UserIdGroupPairs": [{"GroupId": alb_sg}],
        })
        _authorize_ingress(self.ec2, ec2_sg, {
            "IpProtocol": "tcp", "FromPort": 443, "ToPort": 443,
            "IpRanges": [{"CidrIp": vpc_cidr}],
        })

        _step("Bedrock Runtime VPC Endpoint")
        vpce_id = self._ensure_bedrock_endpoint(vpc_id, private_subnets, ec2_sg)
        if enable_knowledge_base:
            self._ensure_vpc_endpoint(
                vpc_id, private_subnets, ec2_sg,
                f"com.amazonaws.{self.region}.bedrock-agent-runtime",
            )

        if enable_knowledge_base and knowledge_base_role_arn and s3_bucket_name:
            ec2_role_arn = f"arn:aws:iam::{self.account_id}:role/{role_name}"
            _step("OpenSearch Serverless Collection 생성")
            opensearch_info = self._create_opensearch_collection(
                ec2_role_arn=ec2_role_arn,
                knowledge_base_role_arn=knowledge_base_role_arn,
            )
            _step("OpenSearch Vector Index 생성")
            self._create_vector_index_in_opensearch(
                opensearch_info["endpoint"],
                self.project,
            )
            _step("Knowledge Base 생성")
            knowledge_base_id = self._create_knowledge_base(
                opensearch_info=opensearch_info,
                knowledge_base_role_arn=knowledge_base_role_arn,
                s3_bucket_name=s3_bucket_name,
            )

        _step("EC2 UserData 렌더링")
        gw_token = secrets.token_hex(32)
        user_data = self._render_user_data(
            gateway_token=gw_token,
            vpc_cidr=vpc_cidr,
            telegram_bot_token=telegram_bot_token,
            telegram_dm_policy=telegram_dm_policy,
            telegram_allow_from=telegram_allow_from,
            telegram_stream_mode=telegram_stream_mode,
            config_path=config_path,
            knowledge_base_id=knowledge_base_id,
            skills_s3_uri=skills_s3_uri,
        )

        _step("EC2 인스턴스 생성 (Private Subnet)")
        inst_id, priv_ip = self._create_ec2(
            subnet_id=private_subnets[0],
            sg_id=ec2_sg,
            profile_arn=profile_arn,
            user_data=user_data,
            key_name=key_name,
            instance_type=instance_type,
            ami_id=ami_id,
            volume_size=volume_size,
        )

        _step("ALB + Target Group + Listener")
        # 기존 CloudFront가 있으면 해당 헤더 값 사용 (재배포 시 일치 유지)
        origin_header_value = (
            self._get_origin_header_from_cloudfront()
            if enable_cloudfront
            else None
        ) or secrets.token_hex(16)
        alb_arn, alb_dns, tg_arn = self._create_alb(
            vpc_id=vpc_id,
            public_subnets=public_subnets,
            alb_sg=alb_sg,
            instance_id=inst_id,
            origin_header_value=origin_header_value if enable_cloudfront else None,
        )

        cf_id, cf_domain = None, None
        if enable_cloudfront:
            _step("CloudFront Distribution")
            cf_id, cf_domain = self._create_cloudfront(
                alb_dns,
                s3_bucket_name=s3_bucket_name if enable_knowledge_base else None,
                origin_header_value=origin_header_value,
            )

        _step("deployment-info.md 생성")
        self.out = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "region": self.region,
            "account_id": self.account_id,
            "vpc_id": vpc_id,
            "vpc_cidr": vpc_cidr,
            "public_subnets": public_subnets,
            "private_subnets": private_subnets,
            "iam_role": role_name,
            "instance_profile": profile_name,
            "ec2_sg": ec2_sg,
            "alb_sg": alb_sg,
            "vpce_id": vpce_id,
            "nat_gateway_id": net.get("nat_gateway_id"),
            "instance_id": inst_id,
            "private_ip": priv_ip,
            "alb_arn": alb_arn,
            "alb_dns": alb_dns,
            "tg_arn": tg_arn,
            "cloudfront_id": cf_id,
            "cloudfront_domain": cf_domain,
            "gateway_token": gw_token,
            "telegram_dm_policy": telegram_dm_policy,
            "telegram_allow_from": telegram_allow_from,
            "telegram_stream_mode": telegram_stream_mode,
            "s3_bucket": s3_bucket_name,
            "knowledge_base_id": knowledge_base_id,
            "opensearch_endpoint": (opensearch_info or {}).get("endpoint"),
        }
        self._write_deployment_info(deployment_info_path)

        return self.out

    # ==================================================================
    # VPC / Networking
    # ==================================================================
    def _ensure_vpc_networking(self) -> Dict[str, Any]:
        vpc_name = f"vpc-for-{self.project}"
        existing = self.ec2.describe_vpcs(
            Filters=[{"Name": "tag:Name", "Values": [vpc_name]}]
        )["Vpcs"]

        if existing:
            return self._reuse_vpc(existing[0])
        return self._create_vpc(vpc_name)

    def _reuse_vpc(self, vpc: Dict) -> Dict[str, Any]:
        vpc_id = vpc["VpcId"]
        vpc_cidr = vpc["CidrBlock"]
        logger.info("  기존 VPC 재사용: %s (%s)", vpc_id, vpc_cidr)

        subs = self.ec2.describe_subnets(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
        )["Subnets"]
        classified = _classify_subnets(self.ec2, subs)
        public_subnets = classified["public"]
        private_subnets = classified["private"]

        existing_cidrs = {s["CidrBlock"] for s in subs}

        azs = self.ec2.describe_availability_zones()["AvailabilityZones"][:2]
        az_names = [az["ZoneName"] for az in azs]

        igw_id = self._get_or_create_igw(vpc_id)

        if len(public_subnets) < 2:
            logger.info("  Public Subnet 부족 → 생성")
            public_rt = self._find_or_create_public_rt(vpc_id, igw_id)
            new_pubs = self._create_subnets(
                vpc_id, az_names, vpc_cidr, existing_cidrs,
                offset=0, tag_prefix="public", count=2 - len(public_subnets),
                map_public=True, route_table_id=public_rt,
            )
            public_subnets.extend(new_pubs)
            existing_cidrs.update(
                s["CidrBlock"]
                for s in self.ec2.describe_subnets(SubnetIds=new_pubs)["Subnets"]
            )

        nat_id = self._get_or_create_nat(vpc_id, public_subnets[0])

        if not private_subnets:
            logger.info("  Private Subnet 없음 → 생성")
            priv_rt = self._find_or_create_private_rt(vpc_id, nat_id)
            private_subnets = self._create_subnets(
                vpc_id, az_names, vpc_cidr, existing_cidrs,
                offset=10, tag_prefix="private", count=2,
                route_table_id=priv_rt,
            )

        self._ensure_main_route_igw(vpc_id, igw_id)

        return {
            "vpc_id": vpc_id,
            "vpc_cidr": vpc_cidr,
            "public_subnets": public_subnets,
            "private_subnets": private_subnets,
            "nat_gateway_id": nat_id,
        }

    def _create_vpc(self, vpc_name: str) -> Dict[str, Any]:
        cidr = _get_available_cidr(self.ec2)
        logger.info("  새 VPC 생성: %s (%s)", vpc_name, cidr)

        vpc_id = self.ec2.create_vpc(
            CidrBlock=cidr,
            TagSpecifications=[{
                "ResourceType": "vpc",
                "Tags": [{"Key": "Name", "Value": vpc_name}],
            }],
        )["Vpc"]["VpcId"]

        self.ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsHostnames={"Value": True})
        self.ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsSupport={"Value": True})

        azs = self.ec2.describe_availability_zones()["AvailabilityZones"][:2]
        az_names = [az["ZoneName"] for az in azs]

        igw_id = self._get_or_create_igw(vpc_id)
        pub_rt = self._find_or_create_public_rt(vpc_id, igw_id)

        existing_cidrs: set[str] = set()
        public_subnets = self._create_subnets(
            vpc_id, az_names, cidr, existing_cidrs,
            offset=0, tag_prefix="public", count=2,
            map_public=True, route_table_id=pub_rt,
        )
        existing_cidrs.update(
            s["CidrBlock"]
            for s in self.ec2.describe_subnets(SubnetIds=public_subnets)["Subnets"]
        )

        nat_id = self._get_or_create_nat(vpc_id, public_subnets[0])

        priv_rt = self._find_or_create_private_rt(vpc_id, nat_id)
        private_subnets = self._create_subnets(
            vpc_id, az_names, cidr, existing_cidrs,
            offset=10, tag_prefix="private", count=2,
            route_table_id=priv_rt,
        )

        return {
            "vpc_id": vpc_id,
            "vpc_cidr": cidr,
            "public_subnets": public_subnets,
            "private_subnets": private_subnets,
            "nat_gateway_id": nat_id,
        }

    # ------------------------------------------------------------------
    # Subnet helpers
    # ------------------------------------------------------------------
    def _create_subnets(
        self,
        vpc_id: str,
        az_names: List[str],
        vpc_cidr: str,
        existing_cidrs: set,
        offset: int,
        tag_prefix: str,
        count: int = 2,
        map_public: bool = False,
        route_table_id: Optional[str] = None,
    ) -> List[str]:
        net = ipaddress.ip_network(vpc_cidr)
        all_sn = list(net.subnets(new_prefix=24))
        result: List[str] = []

        for i in range(count):
            az = az_names[i % len(az_names)]
            sn_cidr = self._pick_cidr(all_sn, existing_cidrs, offset + i)
            if sn_cidr is None:
                logger.warning("  사용 가능한 CIDR 없음, 건너뜀 (%s)", az)
                continue
            try:
                sub = self.ec2.create_subnet(
                    VpcId=vpc_id,
                    CidrBlock=sn_cidr,
                    AvailabilityZone=az,
                    TagSpecifications=[{
                        "ResourceType": "subnet",
                        "Tags": [
                            {"Key": "Name", "Value": f"{tag_prefix}-subnet-{self.project}-{i+1}"},
                        ],
                    }],
                )
                sid = sub["Subnet"]["SubnetId"]
                existing_cidrs.add(sn_cidr)
                _wait_subnet(self.ec2, sid)
                logger.info("  Subnet 생성: %s (%s, %s)", sid, az, sn_cidr)

                if map_public:
                    self.ec2.modify_subnet_attribute(
                        SubnetId=sid, MapPublicIpOnLaunch={"Value": True}
                    )
                if route_table_id:
                    try:
                        self.ec2.associate_route_table(RouteTableId=route_table_id, SubnetId=sid)
                    except ClientError:
                        pass
                result.append(sid)
            except ClientError as exc:
                code = exc.response["Error"]["Code"]
                if code in ("InvalidSubnet.Overlap", "InvalidSubnet.Range"):
                    logger.warning("  CIDR 충돌 %s → 건너뜀", sn_cidr)
                else:
                    raise
        return result

    @staticmethod
    def _pick_cidr(all_sn, existing, preferred_idx) -> Optional[str]:
        if preferred_idx < len(all_sn):
            c = str(all_sn[preferred_idx])
            if c not in existing:
                return c
        for alt in range(len(all_sn)):
            c = str(all_sn[alt])
            if c not in existing:
                return c
        return None

    # ------------------------------------------------------------------
    # IGW / NAT / Route Table helpers
    # ------------------------------------------------------------------
    def _get_or_create_igw(self, vpc_id: str) -> str:
        igws = self.ec2.describe_internet_gateways(
            Filters=[{"Name": "attachment.vpc-id", "Values": [vpc_id]}]
        )["InternetGateways"]
        if igws:
            return igws[0]["InternetGatewayId"]
        igw = self.ec2.create_internet_gateway(
            TagSpecifications=[{
                "ResourceType": "internet-gateway",
                "Tags": [{"Key": "Name", "Value": f"igw-{self.project}"}],
            }]
        )["InternetGateway"]
        igw_id = igw["InternetGatewayId"]
        self.ec2.attach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)
        logger.info("  IGW 생성/연결: %s", igw_id)
        return igw_id

    def _get_or_create_nat(self, vpc_id: str, public_subnet_id: str) -> str:
        nats = self.ec2.describe_nat_gateways(
            Filters=[
                {"Name": "vpc-id", "Values": [vpc_id]},
                {"Name": "state", "Values": ["available", "pending"]},
            ]
        )["NatGateways"]
        for n in nats:
            tags = {t["Key"]: t["Value"] for t in n.get("Tags", [])}
            if tags.get("Name", "").startswith(f"nat-{self.project}"):
                nat_id = n["NatGatewayId"]
                if n["State"] == "pending":
                    _wait_nat(self.ec2, nat_id)
                logger.info("  NAT 재사용: %s", nat_id)
                return nat_id
        if nats:
            nat_id = nats[0]["NatGatewayId"]
            logger.info("  기존 NAT 재사용: %s", nat_id)
            return nat_id

        eip = self.ec2.allocate_address(Domain="vpc")["AllocationId"]
        nat = self.ec2.create_nat_gateway(
            SubnetId=public_subnet_id,
            AllocationId=eip,
            TagSpecifications=[{
                "ResourceType": "natgateway",
                "Tags": [{"Key": "Name", "Value": f"nat-{self.project}"}],
            }],
        )["NatGateway"]
        nat_id = nat["NatGatewayId"]
        logger.info("  NAT 생성: %s (available 대기중...)", nat_id)
        _wait_nat(self.ec2, nat_id)
        return nat_id

    def _find_or_create_public_rt(self, vpc_id: str, igw_id: str) -> str:
        for rt in self.ec2.describe_route_tables(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
        )["RouteTables"]:
            for r in rt["Routes"]:
                if r.get("GatewayId") == igw_id:
                    return rt["RouteTableId"]
        rt_id = self.ec2.create_route_table(
            VpcId=vpc_id,
            TagSpecifications=[{
                "ResourceType": "route-table",
                "Tags": [{"Key": "Name", "Value": f"public-rt-{self.project}"}],
            }],
        )["RouteTable"]["RouteTableId"]
        self.ec2.create_route(
            RouteTableId=rt_id, DestinationCidrBlock="0.0.0.0/0", GatewayId=igw_id
        )
        logger.info("  Public RouteTable 생성: %s", rt_id)
        return rt_id

    def _find_or_create_private_rt(self, vpc_id: str, nat_id: str) -> str:
        for rt in self.ec2.describe_route_tables(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
        )["RouteTables"]:
            for r in rt["Routes"]:
                if r.get("NatGatewayId") == nat_id:
                    return rt["RouteTableId"]
        rt_id = self.ec2.create_route_table(
            VpcId=vpc_id,
            TagSpecifications=[{
                "ResourceType": "route-table",
                "Tags": [{"Key": "Name", "Value": f"private-rt-{self.project}"}],
            }],
        )["RouteTable"]["RouteTableId"]
        try:
            self.ec2.create_route(
                RouteTableId=rt_id, DestinationCidrBlock="0.0.0.0/0", NatGatewayId=nat_id
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "RouteAlreadyExists":
                raise
        logger.info("  Private RouteTable 생성: %s", rt_id)
        return rt_id

    def _ensure_main_route_igw(self, vpc_id: str, igw_id: str) -> None:
        for rt in self.ec2.describe_route_tables(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
        )["RouteTables"]:
            for assoc in rt.get("Associations", []):
                if assoc.get("Main"):
                    has_igw = any(
                        r.get("GatewayId", "").startswith("igw-") for r in rt["Routes"]
                    )
                    if not has_igw:
                        try:
                            self.ec2.create_route(
                                RouteTableId=rt["RouteTableId"],
                                DestinationCidrBlock="0.0.0.0/0",
                                GatewayId=igw_id,
                            )
                        except ClientError:
                            pass
                    return

    # ==================================================================
    # IAM
    # ==================================================================
    def _ensure_iam(
        self,
        knowledge_base_role_arn: Optional[str] = None,
    ) -> tuple[str, str, str]:
        role_name = f"{self.project}-bedrock-role"
        profile_name = f"{self.project}-bedrock-profile"

        trust = {
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"Service": "ec2.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }],
        }
        bedrock_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": [
                        "bedrock:InvokeModel",
                        "bedrock:InvokeModelWithResponseStream",
                        "bedrock:ListFoundationModels",
                        "bedrock:ListKnowledgeBases",
                        "bedrock:GetKnowledgeBase",
                        "bedrock:GetFoundationModel",
                        "bedrock:GetInferenceProfile",
                        "bedrock:Retrieve",
                    ],
                    "Resource": "*",
                },
                {
                    "Effect": "Allow",
                    "Action": [
                        "s3:ListAllMyBuckets",
                        "s3:ListBucket",
                        "s3:GetObject",
                        "s3:PutObject",
                        "s3:DeleteObject",
                        "s3:GetBucketLocation",
                    ],
                    "Resource": "*",
                },
                {
                    "Effect": "Allow",
                    "Action": ["cloudfront:ListDistributions"],
                    "Resource": "*",
                },
            ],
        }

        # Knowledge Base 관련 정책 추가
        if knowledge_base_role_arn:
            bedrock_policy["Statement"].extend([
                {
                    "Effect": "Allow",
                    "Action": ["aoss:APIAccessAll"],
                    "Resource": ["*"],
                },
                {
                    "Effect": "Allow",
                    "Action": ["iam:PassRole"],
                    "Resource": [knowledge_base_role_arn],
                },
            ])

        managed_policies = [
            "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
        ]

        # --- Instance Profile 확보 ---
        try:
            self.iam.create_instance_profile(InstanceProfileName=profile_name)
            logger.info("  Profile 생성: %s", profile_name)
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "EntityAlreadyExists":
                raise
            logger.info("  Profile 재사용: %s", profile_name)

        time.sleep(2)
        profile = self.iam.get_instance_profile(InstanceProfileName=profile_name)["InstanceProfile"]
        profile_arn = profile["Arn"]
        existing_roles = [r["RoleName"] for r in profile["Roles"]]

        # --- Role 결정: Profile에 이미 연결된 Role이 있으면 그대로 사용 ---
        if existing_roles:
            role_name = existing_roles[0]
            logger.info("  기존 Role 업데이트: %s", role_name)
        else:
            try:
                self.iam.create_role(
                    RoleName=role_name,
                    AssumeRolePolicyDocument=json.dumps(trust),
                    Description="OpenClaw Bedrock EC2 role",
                )
                logger.info("  Role 생성: %s", role_name)
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "EntityAlreadyExists":
                    raise
                logger.info("  Role 재사용: %s", role_name)

            self.iam.add_role_to_instance_profile(
                InstanceProfileName=profile_name, RoleName=role_name,
            )

        # --- Trust Policy / Inline Policy / Managed Policy 업데이트 ---
        self.iam.update_assume_role_policy(
            RoleName=role_name, PolicyDocument=json.dumps(trust),
        )
        self.iam.put_role_policy(
            RoleName=role_name, PolicyName="BedrockAccess",
            PolicyDocument=json.dumps(bedrock_policy),
        )
        logger.info("  인라인 정책 업데이트: BedrockAccess → %s", role_name)

        try:
            attached = self.iam.list_attached_role_policies(RoleName=role_name)
            current_arns = {p["PolicyArn"] for p in attached["AttachedPolicies"]}
            for policy_arn in managed_policies:
                if policy_arn not in current_arns:
                    self.iam.attach_role_policy(RoleName=role_name, PolicyArn=policy_arn)
                    logger.info("  매니지드 정책 추가: %s", policy_arn)
        except ClientError:
            pass

        # IAM eventual consistency
        logger.info("  IAM Profile 전파 대기중...")
        for attempt in range(20):
            try:
                resp = self.iam.get_instance_profile(InstanceProfileName=profile_name)
                roles = resp["InstanceProfile"].get("Roles", [])
                if attempt >= 5 and any(r["RoleName"] == role_name for r in roles):
                    break
            except ClientError:
                pass
            time.sleep(2)

        return role_name, profile_name, profile_arn

    # ==================================================================
    # Knowledge Base (S3, OpenSearch, KB)
    # ==================================================================
    def _create_s3_bucket(self) -> str:
        """Knowledge Base용 S3 버킷 생성."""
        bucket_name = f"storage-for-{self.project}-{self.account_id}-{self.region}"
        try:
            if self.region == "us-east-1":
                self.s3.create_bucket(Bucket=bucket_name)
            else:
                self.s3.create_bucket(
                    Bucket=bucket_name,
                    CreateBucketConfiguration={"LocationConstraint": self.region},
                )
            logger.info("  S3 버킷 생성: %s", bucket_name)
        except ClientError as exc:
            if exc.response["Error"]["Code"] not in ("BucketAlreadyExists", "BucketAlreadyOwnedByYou"):
                raise
            logger.info("  S3 버킷 재사용: %s", bucket_name)

        self.s3.put_public_access_block(
            Bucket=bucket_name,
            PublicAccessBlockConfiguration={
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True,
            },
        )
        for folder in ["docs/", "artifacts/"]:
            try:
                self.s3.put_object(Bucket=bucket_name, Key=folder, Body=b"")
            except ClientError:
                pass
        return bucket_name

    def _upload_skills_to_s3(self, bucket_name: str, skills_path: Path) -> None:
        """로컬 skills 폴더를 S3 artifacts/skills/ 에 업로드."""
        prefix = "artifacts/skills"
        count = 0
        for p in skills_path.rglob("*"):
            if p.is_file():
                rel = p.relative_to(skills_path)
                key = f"{prefix}/{rel}"
                self.s3.upload_file(str(p), bucket_name, key)
                count += 1
        logger.info("  Skills 업로드: %d개 파일 → s3://%s/%s/", count, bucket_name, prefix)

    def _ensure_knowledge_base_role(self) -> str:
        """Knowledge Base용 IAM Role 생성 (Bedrock이 S3/OpenSearch 접근)."""
        role_name = f"role-knowledge-base-for-{self.project}-{self.region}"
        trust = {
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"Service": "bedrock.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }],
        }
        try:
            self.iam.create_role(
                RoleName=role_name,
                AssumeRolePolicyDocument=json.dumps(trust),
                Description=f"Knowledge Base role for {self.project}",
            )
            logger.info("  KB Role 생성: %s", role_name)
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "EntityAlreadyExists":
                raise
            self.iam.update_assume_role_policy(
                RoleName=role_name, PolicyDocument=json.dumps(trust),
            )
            logger.info("  KB Role 재사용: %s", role_name)

        s3_policy = {
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Action": ["s3:*"], "Resource": ["*"]}],
        }
        aoss_policy = {
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Action": ["aoss:APIAccessAll"], "Resource": ["*"]}],
        }
        bedrock_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": ["bedrock:GetInferenceProfile", "bedrock:InvokeModel"],
                    "Resource": [
                        f"arn:aws:bedrock:{self.region}:{self.account_id}:inference-profile/*",
                        f"arn:aws:bedrock:{self.region}:*:inference-profile/*",
                        "arn:aws:bedrock:*::foundation-model/*",
                    ],
                },
            ],
        }
        self.iam.put_role_policy(
            RoleName=role_name, PolicyName="kb-s3-policy",
            PolicyDocument=json.dumps(s3_policy),
        )
        self.iam.put_role_policy(
            RoleName=role_name, PolicyName="kb-opensearch-policy",
            PolicyDocument=json.dumps(aoss_policy),
        )
        self.iam.put_role_policy(
            RoleName=role_name, PolicyName="kb-bedrock-policy",
            PolicyDocument=json.dumps(bedrock_policy),
        )
        role = self.iam.get_role(RoleName=role_name)
        return role["Role"]["Arn"]

    def _create_opensearch_collection(
        self,
        ec2_role_arn: str,
        knowledge_base_role_arn: str,
    ) -> Dict[str, str]:
        """OpenSearch Serverless Collection 생성."""
        collection_name = self.project
        enc_name = f"enc-{self.project}-{self.region}"
        net_name = f"net-{self.project}-{self.region}"
        data_name = f"data-{self.project}"

        # 기존 컬렉션 확인
        for c in self.opensearch.list_collections().get("collectionSummaries", []):
            if c.get("name") == collection_name and c.get("status") == "ACTIVE":
                detail = self.opensearch.batch_get_collection(names=[collection_name])["collectionDetails"][0]
                endpoint = detail.get("collectionEndpoint")
                if not endpoint:
                    for _ in range(60):
                        time.sleep(10)
                        detail = self.opensearch.batch_get_collection(names=[collection_name])["collectionDetails"][0]
                        endpoint = detail.get("collectionEndpoint")
                        if endpoint and detail.get("status") == "ACTIVE":
                            break
                logger.info("  OpenSearch Collection 재사용: %s", collection_name)
                return {"arn": detail["arn"], "endpoint": endpoint}

        # Encryption policy
        try:
            self.opensearch.create_security_policy(
                name=enc_name, type="encryption",
                description=f"Encryption for {self.project}",
                policy=json.dumps({"Rules": [{"ResourceType": "collection", "Resource": [f"collection/{collection_name}"]}], "AWSOwnedKey": True}),
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConflictException":
                raise

        # Network policy
        try:
            self.opensearch.create_security_policy(
                name=net_name, type="network",
                description=f"Network for {self.project}",
                policy=json.dumps([{
                    "Rules": [
                        {"ResourceType": "dashboard", "Resource": [f"collection/{collection_name}"]},
                        {"ResourceType": "collection", "Resource": [f"collection/{collection_name}"]},
                    ],
                    "AllowFromPublic": True,
                }]),
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConflictException":
                raise

        # Data access policy
        principals = [f"arn:aws:iam::{self.account_id}:root", ec2_role_arn, knowledge_base_role_arn]
        data_policy = [{
            "Rules": [
                {"Resource": [f"collection/{collection_name}"], "Permission": ["aoss:CreateCollectionItems", "aoss:DeleteCollectionItems", "aoss:UpdateCollectionItems", "aoss:DescribeCollectionItems"], "ResourceType": "collection"},
                {"Resource": [f"index/{collection_name}/*"], "Permission": ["aoss:CreateIndex", "aoss:DeleteIndex", "aoss:UpdateIndex", "aoss:DescribeIndex", "aoss:ReadDocument", "aoss:WriteDocument"], "ResourceType": "index"},
            ],
            "Principal": principals,
        }]
        try:
            self.opensearch.create_access_policy(name=data_name, type="data", policy=json.dumps(data_policy))
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConflictException":
                raise

        time.sleep(5)
        resp = self.opensearch.create_collection(
            name=collection_name,
            description=f"OpenSearch for {self.project}",
            type="VECTORSEARCH",
        )
        arn = resp["createCollectionDetail"]["arn"]
        logger.info("  OpenSearch Collection 생성: %s (ACTIVE 대기중...)", collection_name)
        for _ in range(60):
            time.sleep(10)
            detail = self.opensearch.batch_get_collection(names=[collection_name])["collectionDetails"][0]
            if detail.get("status") == "ACTIVE" and detail.get("collectionEndpoint"):
                return {"arn": arn, "endpoint": detail["collectionEndpoint"]}
        raise RuntimeError("OpenSearch Collection ACTIVE 대기 시간 초과")

    def _create_vector_index_in_opensearch(self, collection_endpoint: str, index_name: str) -> None:
        """OpenSearch에 vector index 생성."""
        if not collection_endpoint or not collection_endpoint.strip():
            raise ValueError("collection_endpoint 필요")
        if not collection_endpoint.startswith(("http://", "https://")):
            collection_endpoint = f"https://{collection_endpoint}"

        try:
            import requests
            from requests_aws4auth import AWS4Auth
        except ImportError:
            import subprocess
            import sys
            subprocess.check_call([sys.executable, "-m", "pip", "install", "requests-aws4auth"])
            import requests
            from requests_aws4auth import AWS4Auth

        creds = self.session.get_credentials()
        auth = AWS4Auth(creds.access_key, creds.secret_key, self.region, "aoss", session_token=creds.token)
        url = f"{collection_endpoint.rstrip('/')}/{index_name}"

        r = requests.get(url, auth=auth, timeout=30)
        if r.status_code == 200:
            logger.info("  Vector index 이미 존재: %s", index_name)
            return

        mapping = {
            "settings": {"index": {"knn": True, "knn.algo_param.ef_search": 512}},
            "mappings": {
                "properties": {
                    "vector_field": {"type": "knn_vector", "dimension": 1024, "method": {"name": "hnsw", "space_type": "cosinesimil", "engine": "faiss", "parameters": {"ef_construction": 512, "m": 16}}},
                    "AMAZON_BEDROCK_TEXT": {"type": "text"},
                    "AMAZON_BEDROCK_METADATA": {"type": "text"},
                }
            },
        }
        r = requests.put(url, auth=auth, headers={"Content-Type": "application/json"}, data=json.dumps(mapping), timeout=30)
        if r.status_code not in (200, 201):
            raise RuntimeError(f"Vector index 생성 실패: {r.status_code} - {r.text}")
        logger.info("  Vector index 생성: %s", index_name)
        time.sleep(30)

    def _create_knowledge_base(
        self,
        opensearch_info: Dict[str, str],
        knowledge_base_role_arn: str,
        s3_bucket_name: str,
    ) -> str:
        """Knowledge Base 생성 (OpenSearch + S3 데이터 소스)."""
        for kb in self.bedrock_agent.list_knowledge_bases().get("knowledgeBaseSummaries", []):
            if kb.get("name") == self.project:
                coll_arn = self.bedrock_agent.get_knowledge_base(knowledgeBaseId=kb["knowledgeBaseId"])["knowledgeBase"]["storageConfiguration"]["opensearchServerlessConfiguration"]["collectionArn"]
                if coll_arn == opensearch_info["arn"]:
                    logger.info("  Knowledge Base 재사용: %s", kb["knowledgeBaseId"])
                    return kb["knowledgeBaseId"]

        resp = self.bedrock_agent.create_knowledge_base(
            name=self.project,
            description="Knowledge base based on OpenSearch",
            roleArn=knowledge_base_role_arn,
            knowledgeBaseConfiguration={
                "type": "VECTOR",
                "vectorKnowledgeBaseConfiguration": {
                    "embeddingModelArn": f"arn:aws:bedrock:{self.region}::foundation-model/amazon.titan-embed-text-v2:0",
                    "embeddingModelConfiguration": {"bedrockEmbeddingModelConfiguration": {"dimensions": 1024}},
                },
            },
            storageConfiguration={
                "type": "OPENSEARCH_SERVERLESS",
                "opensearchServerlessConfiguration": {
                    "collectionArn": opensearch_info["arn"],
                    "fieldMapping": {"metadataField": "AMAZON_BEDROCK_METADATA", "textField": "AMAZON_BEDROCK_TEXT", "vectorField": "vector_field"},
                    "vectorIndexName": self.project,
                },
            },
        )
        kb_id = resp["knowledgeBase"]["knowledgeBaseId"]
        logger.info("  Knowledge Base 생성: %s (ACTIVE 대기중...)", kb_id)
        while True:
            status = self.bedrock_agent.get_knowledge_base(knowledgeBaseId=kb_id)["knowledgeBase"]["status"]
            if status == "ACTIVE":
                break
            if status == "FAILED":
                raise RuntimeError("Knowledge Base 생성 실패")
            time.sleep(10)

        self.bedrock_agent.create_data_source(
            knowledgeBaseId=kb_id,
            name=s3_bucket_name,
            description=f"S3 data source: {s3_bucket_name}",
            dataDeletionPolicy="RETAIN",
            dataSourceConfiguration={
                "type": "S3",
                "s3Configuration": {"bucketArn": f"arn:aws:s3:::{s3_bucket_name}", "inclusionPrefixes": ["docs/"]},
            },
            vectorIngestionConfiguration={
                "chunkingConfiguration": {
                    "chunkingStrategy": "HIERARCHICAL",
                    "hierarchicalChunkingConfiguration": {"levelConfigurations": [{"maxTokens": 1500}, {"maxTokens": 300}], "overlapTokens": 60},
                },
                "parsingConfiguration": {
                    "parsingStrategy": "BEDROCK_FOUNDATION_MODEL",
                    "bedrockFoundationModelConfiguration": {"modelArn": f"arn:aws:bedrock:{self.region}:{self.account_id}:inference-profile/global.anthropic.claude-sonnet-4-5-20250929-v1:0"},
                },
            },
        )
        logger.info("  Data Source 생성 완료 (docs/ prefix)")
        return kb_id

    # ==================================================================
    # VPC Endpoint
    # ==================================================================
    def _ensure_bedrock_endpoint(self, vpc_id: str, private_subnets: List[str], sg_id: str) -> str:
        svc = f"com.amazonaws.{self.region}.bedrock-runtime"
        eps = self.ec2.describe_vpc_endpoints(
            Filters=[
                {"Name": "vpc-id", "Values": [vpc_id]},
                {"Name": "service-name", "Values": [svc]},
            ]
        )["VpcEndpoints"]
        if eps:
            eid = eps[0]["VpcEndpointId"]
            logger.info("  VPC Endpoint 재사용: %s", eid)
            return eid
        resp = self.ec2.create_vpc_endpoint(
            VpcId=vpc_id, ServiceName=svc, VpcEndpointType="Interface",
            SubnetIds=private_subnets, SecurityGroupIds=[sg_id],
            PrivateDnsEnabled=True,
        )
        eid = resp["VpcEndpoint"]["VpcEndpointId"]
        logger.info("  VPC Endpoint 생성: %s", eid)
        return eid

    def _ensure_vpc_endpoint(
        self, vpc_id: str, private_subnets: List[str], sg_id: str, service_name: str
    ) -> str:
        """VPC Endpoint 생성 또는 재사용."""
        eps = self.ec2.describe_vpc_endpoints(
            Filters=[
                {"Name": "vpc-id", "Values": [vpc_id]},
                {"Name": "service-name", "Values": [service_name]},
            ]
        )["VpcEndpoints"]
        if eps:
            eid = eps[0]["VpcEndpointId"]
            logger.info("  VPC Endpoint 재사용: %s (%s)", eid, service_name.split(".")[-1])
            return eid
        resp = self.ec2.create_vpc_endpoint(
            VpcId=vpc_id, ServiceName=service_name, VpcEndpointType="Interface",
            SubnetIds=private_subnets, SecurityGroupIds=[sg_id],
            PrivateDnsEnabled=True,
        )
        eid = resp["VpcEndpoint"]["VpcEndpointId"]
        logger.info("  VPC Endpoint 생성: %s (%s)", eid, service_name.split(".")[-1])
        return eid

    # ==================================================================
    # UserData
    # ==================================================================
    def _render_user_data(
        self,
        gateway_token: str,
        vpc_cidr: str,
        telegram_bot_token: str,
        telegram_dm_policy: str,
        telegram_allow_from: List[str],
        telegram_stream_mode: str,
        config_path: Path,
        knowledge_base_id: Optional[str] = None,
        skills_s3_uri: Optional[str] = None,
    ) -> str:
        config = self._build_openclaw_config(
            gateway_token, vpc_cidr,
            telegram_bot_token, telegram_dm_policy,
            telegram_allow_from, telegram_stream_mode,
            config_path,
            knowledge_base_id=knowledge_base_id,
        )
        config_json = json.dumps(config, ensure_ascii=False, indent=2)

        lines = [
            "#!/bin/bash",
            "set -euo pipefail",
            "exec > >(tee /var/log/openclaw-install.log)",
            "exec 2>&1",
            "",
            'echo "=== OpenClaw install start ==="',
            "",
            "dnf remove -y nodejs nodejs-full-i18n npm || true",
            "curl -fsSL https://rpm.nodesource.com/setup_22.x | bash -",
            "dnf install -y nodejs git --allowerasing",
            "",
            "timedatectl set-timezone Asia/Seoul || true",
            "npm install -g openclaw@latest",
            "",
            "mkdir -p /home/ec2-user/.openclaw /home/ec2-user/clawd /home/ec2-user/clawd/skills",
            "cat > /home/ec2-user/.openclaw/openclaw.json <<'OCJSON'",
            config_json,
            "OCJSON",
            "",
            f'echo "{gateway_token}" > /home/ec2-user/openclaw-token.txt',
            "chown -R ec2-user:ec2-user /home/ec2-user/.openclaw /home/ec2-user/clawd /home/ec2-user/openclaw-token.txt",
            "",
        ]
        if skills_s3_uri:
            lines.extend([
                'echo "=== Skills S3에서 복사 중 ==="',
                "for i in 1 2 3 4 5; do",
                f'  aws s3 cp "{skills_s3_uri}" /home/ec2-user/clawd/skills/ --recursive && break',
                '  echo "Skills 복사 재시도 $i/5 (IAM 전파 대기)..."',
                "  sleep 10",
                "done",
                "ls -la /home/ec2-user/clawd/skills/ 2>/dev/null || true",
                "chown -R ec2-user:ec2-user /home/ec2-user/clawd/skills",
                "",
            ])
        lines.extend([
            "cat > /etc/systemd/system/openclaw-gateway.service <<'SVC'",
            "[Unit]",
            "Description=OpenClaw Gateway Service",
            "After=network.target",
            "",
            "[Service]",
            "Type=simple",
            "User=ec2-user",
            "WorkingDirectory=/home/ec2-user",
            'Environment="PATH=/usr/bin:/usr/local/bin"',
            'Environment="NODE_ENV=production"',
            f'Environment="AWS_REGION={self.region}"',
            f"ExecStart=/usr/bin/env openclaw gateway run --bind lan --port {GATEWAY_PORT} --token {gateway_token}",
            "Restart=always",
            "RestartSec=10",
            "StandardOutput=journal",
            "StandardError=journal",
            "SyslogIdentifier=openclaw-gateway",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
            "SVC",
            "",
            "systemctl daemon-reload",
            "systemctl enable openclaw-gateway.service",
            "systemctl start openclaw-gateway.service",
            "",
            'echo "=== OpenClaw install done ==="',
        ])
        return "\n".join(lines) + "\n"

    def _build_openclaw_config(
        self,
        gateway_token: str,
        vpc_cidr: str,
        telegram_bot_token: str,
        telegram_dm_policy: str,
        telegram_allow_from: List[str],
        telegram_stream_mode: str,
        config_path: Path,
        knowledge_base_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if config_path.exists():
            config = json.loads(config_path.read_text("utf-8"))
        else:
            logger.warning("  설정 파일 없음 (%s) → 기본값 사용", config_path)
            config = {}

        gw = config.setdefault("gateway", {})
        gw["port"] = GATEWAY_PORT
        gw["mode"] = "local"
        gw["bind"] = "lan"
        gw["trustedProxies"] = [vpc_cidr]
        gw.setdefault("auth", {})["token"] = gateway_token
        ui = gw.setdefault("controlUi", {})
        ui["dangerouslyAllowHostHeaderOriginFallback"] = True

        if "models" not in config:
            config["models"] = {
                "providers": {
                    "amazon-bedrock": {
                        "baseUrl": f"https://bedrock-runtime.{self.region}.amazonaws.com",
                        "auth": "aws-sdk",
                        "api": "bedrock-converse-stream",
                        "models": [
                            {
                                "id": "global.anthropic.claude-sonnet-4-5-20250929-v1:0",
                                "name": "Claude Sonnet 4.5",
                                "reasoning": True,
                                "input": ["text", "image"],
                                "cost": {"input": 0.003, "output": 0.015},
                                "contextWindow": 200000,
                                "maxTokens": 8192,
                            },
                            {
                                "id": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
                                "name": "Claude Haiku 4.5",
                                "reasoning": False,
                                "input": ["text", "image"],
                                "cost": {"input": 0.0008, "output": 0.004},
                                "contextWindow": 200000,
                                "maxTokens": 8192,
                            },
                            {
                                "id": "global.anthropic.claude-opus-4-6-v1",
                                "name": "Claude Opus 4.6",
                                "reasoning": True,
                                "input": ["text", "image"],
                                "cost": {"input": 0.015, "output": 0.075},
                                "contextWindow": 200000,
                                "maxTokens": 32000,
                            },
                            {
                                "id": "global.anthropic.claude-sonnet-4-6",
                                "name": "Claude Sonnet 4.6",
                                "reasoning": True,
                                "input": ["text", "image"],
                                "cost": {"input": 0.003, "output": 0.015},
                                "contextWindow": 200000,
                                "maxTokens": 16384,
                            },
                        ],
                    }
                }
            }

        ag = config.setdefault("agents", {}).setdefault("defaults", {})
        ag["workspace"] = "/home/ec2-user/clawd"
        # knowledgeBaseId는 OpenClaw가 agents.defaults에서 지원하지 않음 (Config invalid 오류)
        # Knowledge Base ID는 deployment-info.md에 기록되며, skill 등으로 별도 설정 필요
        if "model" not in ag:
            ag["model"] = {
                "primary": "amazon-bedrock/global.anthropic.claude-sonnet-4-5-20250929-v1:0"
            }
        comp = ag.setdefault("compaction", {})
        comp.setdefault("reserveTokensFloor", 20000)
        mf = comp.setdefault("memoryFlush", {})
        mf.setdefault("enabled", True)
        mf.setdefault("softThresholdTokens", 4000)

        tg = config.setdefault("channels", {}).setdefault("telegram", {})
        tg["enabled"] = True
        tg["botToken"] = telegram_bot_token
        tg["dmPolicy"] = telegram_dm_policy
        tg["allowFrom"] = telegram_allow_from
        # OpenClaw 최신 버전: streamMode → streaming
        tg["streaming"] = telegram_stream_mode

        config.get("channels", {}).pop("whatsapp", None)

        return config

    # ==================================================================
    # EC2
    # ==================================================================
    def _find_existing_ec2(self) -> Optional[tuple[str, str]]:
        """이름(Name 태그)으로 기존 running/stopped EC2 검색."""
        resp = self.ec2.describe_instances(Filters=[
            {"Name": "tag:Name", "Values": [self.project]},
            {"Name": "instance-state-name", "Values": ["running", "stopped", "pending"]},
        ])
        for resv in resp.get("Reservations", []):
            for inst in resv.get("Instances", []):
                inst_id = inst["InstanceId"]
                priv_ip = inst.get("PrivateIpAddress", "")
                state = inst["State"]["Name"]
                return inst_id, priv_ip, state  # type: ignore[return-value]
        return None

    def _create_ec2(
        self,
        subnet_id: str,
        sg_id: str,
        profile_arn: str,
        user_data: str,
        key_name: Optional[str],
        instance_type: str,
        ami_id: str,
        volume_size: int,
    ) -> tuple[str, str]:
        existing = self._find_existing_ec2()
        if existing:
            inst_id, priv_ip, state = existing
            logger.info("  기존 EC2 발견: %s (%s, %s)", inst_id, state, priv_ip)
            if state == "stopped":
                logger.info("  stopped 상태 → 시작합니다...")
                self.ec2.start_instances(InstanceIds=[inst_id])
                self.ec2.get_waiter("instance_running").wait(InstanceIds=[inst_id])
                priv_ip = self.ec2.describe_instances(InstanceIds=[inst_id])[
                    "Reservations"][0]["Instances"][0]["PrivateIpAddress"]
            elif state == "pending":
                logger.info("  pending 상태 → running 대기중...")
                self.ec2.get_waiter("instance_running").wait(InstanceIds=[inst_id])
                priv_ip = self.ec2.describe_instances(InstanceIds=[inst_id])[
                    "Reservations"][0]["Instances"][0]["PrivateIpAddress"]
            logger.info("  EC2 재사용: %s (IP: %s)", inst_id, priv_ip)
            return inst_id, priv_ip

        params: Dict[str, Any] = dict(
            ImageId=ami_id,
            InstanceType=instance_type,
            SubnetId=subnet_id,
            SecurityGroupIds=[sg_id],
            IamInstanceProfile={"Arn": profile_arn},
            BlockDeviceMappings=[{
                "DeviceName": "/dev/xvda",
                "Ebs": {"VolumeSize": volume_size, "VolumeType": "gp3"},
            }],
            TagSpecifications=[{
                "ResourceType": "instance",
                "Tags": [{"Key": "Name", "Value": self.project}],
            }],
            UserData=user_data,
            MinCount=1,
            MaxCount=1,
        )
        if key_name:
            params["KeyName"] = key_name

        # IAM Profile 전파 지연에 대비한 재시도
        last_err = None
        for attempt in range(10):
            try:
                inst_id = self.ec2.run_instances(**params)["Instances"][0]["InstanceId"]
                break
            except ClientError as exc:
                if "Invalid IAM Instance Profile" in str(exc) and attempt < 9:
                    last_err = exc
                    wait = min((attempt + 1) * 5, 30)
                    logger.warning("  IAM Profile 미전파, %d초 후 재시도... (%d/%d)", wait, attempt + 1, 9)
                    time.sleep(wait)
                else:
                    raise
        else:
            raise last_err  # type: ignore[misc]
        logger.info("  EC2 인스턴스: %s (running 대기중...)", inst_id)
        self.ec2.get_waiter("instance_running").wait(InstanceIds=[inst_id])
        priv_ip = self.ec2.describe_instances(InstanceIds=[inst_id])[
            "Reservations"
        ][0]["Instances"][0]["PrivateIpAddress"]
        logger.info("  EC2 Private IP: %s", priv_ip)
        return inst_id, priv_ip

    # ==================================================================
    # ALB
    # ==================================================================
    def _create_alb(
        self,
        vpc_id: str,
        public_subnets: List[str],
        alb_sg: str,
        instance_id: str,
        origin_header_value: Optional[str] = None,
    ) -> tuple[str, str, str]:
        alb_name = f"alb-{self.project}"[:32]
        tg_name = f"tg-{self.project}"[:32]

        try:
            existing = self.elbv2.describe_load_balancers(Names=[alb_name])
            if existing["LoadBalancers"]:
                alb = existing["LoadBalancers"][0]
                logger.info("  ALB 재사용: %s", alb["DNSName"])
                alb_arn = alb["LoadBalancerArn"]
                alb_dns = alb["DNSName"]
                tgs = self.elbv2.describe_target_groups(
                    LoadBalancerArn=alb_arn
                )["TargetGroups"]
                if tgs:
                    tg_arn = tgs[0]["TargetGroupArn"]
                    self._ensure_target_registered(tg_arn, instance_id)
                    if origin_header_value:
                        self._ensure_alb_custom_header_rule(
                            alb_arn, tg_arn, origin_header_value,
                        )
                else:
                    tg_arn = ""
                return alb_arn, alb_dns, tg_arn
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "LoadBalancerNotFound":
                raise

        alb = self.elbv2.create_load_balancer(
            Name=alb_name,
            Subnets=public_subnets,
            SecurityGroups=[alb_sg],
            Scheme="internet-facing",
            Type="application",
            Tags=[{"Key": "Name", "Value": alb_name}],
        )["LoadBalancers"][0]
        alb_arn = alb["LoadBalancerArn"]
        alb_dns = alb["DNSName"]
        logger.info("  ALB 생성: %s", alb_dns)

        tg = self.elbv2.create_target_group(
            Name=tg_name,
            Protocol="HTTP",
            Port=GATEWAY_PORT,
            VpcId=vpc_id,
            TargetType="instance",
            HealthCheckPath="/",
            HealthCheckProtocol="HTTP",
            Matcher={"HttpCode": "200-401"},
        )["TargetGroups"][0]
        tg_arn = tg["TargetGroupArn"]

        self.elbv2.register_targets(
            TargetGroupArn=tg_arn,
            Targets=[{"Id": instance_id, "Port": GATEWAY_PORT}],
        )
        # Custom header 사용 시: CloudFront만 ALB 통과. 기본값 403으로 직접 접근 차단
        if origin_header_value:
            self.elbv2.create_listener(
                LoadBalancerArn=alb_arn,
                Protocol="HTTP",
                Port=80,
                DefaultActions=[{
                    "Type": "fixed-response",
                    "FixedResponseConfig": {
                        "StatusCode": "403",
                        "ContentType": "text/plain",
                        "MessageBody": "Access denied",
                    },
                }],
            )
            self._ensure_alb_custom_header_rule(alb_arn, tg_arn, origin_header_value)
        else:
            self.elbv2.create_listener(
                LoadBalancerArn=alb_arn,
                Protocol="HTTP",
                Port=80,
                DefaultActions=[{"Type": "forward", "TargetGroupArn": tg_arn}],
            )
        return alb_arn, alb_dns, tg_arn

    def _ensure_alb_custom_header_rule(
        self, alb_arn: str, tg_arn: str, origin_header_value: str,
    ) -> None:
        """ALB Listener에 Custom Header 규칙 추가 (CloudFront 요청만 포워딩)."""
        listeners = self.elbv2.describe_listeners(LoadBalancerArn=alb_arn)
        listener_arn = None
        for lst in listeners.get("Listeners", []):
            if lst.get("Port") == 80 and lst.get("Protocol") == "HTTP":
                listener_arn = lst["ListenerArn"]
                break
        if not listener_arn:
            return

        rules = self.elbv2.describe_rules(ListenerArn=listener_arn)
        rule_exists = False
        for rule in rules.get("Rules", []):
            if rule.get("Priority") == "10":
                for cond in rule.get("Conditions", []):
                    if (cond.get("Field") == "http-header" and
                        cond.get("HttpHeaderConfig", {}).get("HttpHeaderName") == CUSTOM_HEADER_NAME):
                        rule_exists = True
                        break
                break

        if not rule_exists:
            try:
                self.elbv2.create_rule(
                    ListenerArn=listener_arn,
                    Priority=10,
                    Conditions=[{
                        "Field": "http-header",
                        "HttpHeaderConfig": {
                            "HttpHeaderName": CUSTOM_HEADER_NAME,
                            "Values": [origin_header_value],
                        },
                    }],
                    Actions=[{"Type": "forward", "TargetGroupArn": tg_arn}],
                )
                logger.info("  ALB Custom Header 규칙 추가: %s", CUSTOM_HEADER_NAME)
            except ClientError as exc:
                if exc.response["Error"]["Code"] not in ("PriorityInUse", "RuleAlreadyExists"):
                    raise

        # 기존 default action이 forward인 경우 403으로 변경
        listener = next(
            (l for l in listeners.get("Listeners", [])
             if l.get("ListenerArn") == listener_arn),
            None,
        )
        if listener and listener.get("DefaultActions"):
            default = listener["DefaultActions"][0]
            if default.get("Type") == "forward":
                try:
                    self.elbv2.modify_listener(
                        ListenerArn=listener_arn,
                        DefaultActions=[{
                            "Type": "fixed-response",
                            "FixedResponseConfig": {
                                "StatusCode": "403",
                                "ContentType": "text/plain",
                                "MessageBody": "Access denied",
                            },
                        }],
                    )
                    logger.info("  ALB 기본 action: 403 (직접 접근 차단)")
                except ClientError:
                    pass

    def _ensure_target_registered(self, tg_arn: str, instance_id: str) -> None:
        """Target Group에 인스턴스가 등록되어 있지 않으면 등록한다."""
        health = self.elbv2.describe_target_health(TargetGroupArn=tg_arn)
        registered = {t["Target"]["Id"] for t in health["TargetHealthDescriptions"]}
        if instance_id not in registered:
            logger.info("  Target Group에 EC2 등록: %s", instance_id)
            self.elbv2.register_targets(
                TargetGroupArn=tg_arn,
                Targets=[{"Id": instance_id, "Port": GATEWAY_PORT}],
            )
        self.elbv2.modify_target_group(
            TargetGroupArn=tg_arn,
            Matcher={"HttpCode": "200-401"},
        )

    # ==================================================================
    # CloudFront
    # ==================================================================
    def _find_existing_cloudfront(self) -> Optional[tuple[str, str]]:
        """Comment 필드로 기존 CloudFront 배포를 검색."""
        comment = f"{self.project} CloudFront"
        paginator = self.cf.get_paginator("list_distributions")
        for page in paginator.paginate():
            dist_list = page.get("DistributionList", {})
            for item in dist_list.get("Items", []):
                if item.get("Comment") == comment:
                    return item["Id"], item["DomainName"]
        return None

    def _get_origin_header_from_cloudfront(self) -> Optional[str]:
        """기존 CloudFront ALB Origin의 Custom Header 값 반환 (재배포 시 일치용)."""
        existing = self._find_existing_cloudfront()
        if not existing:
            return None
        try:
            resp = self.cf.get_distribution_config(Id=existing[0])
            for origin in resp["DistributionConfig"]["Origins"].get("Items", []):
                if "CustomOriginConfig" not in origin:
                    continue
                for h in origin.get("CustomHeaders", {}).get("Items", []):
                    if h.get("HeaderName") == CUSTOM_HEADER_NAME:
                        return h.get("HeaderValue")
        except ClientError:
            pass
        return None

    def _create_cloudfront(
        self,
        alb_dns: str,
        s3_bucket_name: Optional[str] = None,
        origin_header_value: Optional[str] = None,
    ) -> tuple[str, str]:
        existing = self._find_existing_cloudfront()
        if existing:
            did, dom = existing
            logger.info("  기존 CloudFront 발견: %s (%s)", dom, did)
            if origin_header_value:
                self._ensure_cloudfront_origin_header(did, origin_header_value)
            if s3_bucket_name:
                self._ensure_cloudfront_s3_origin(did, s3_bucket_name)
            return did, dom

        caller_ref = f"{self.project}-{int(time.time())}"
        origin_id_alb = "openclaw-alb-origin"
        alb_origin: Dict[str, Any] = {
            "Id": origin_id_alb,
            "DomainName": alb_dns,
            "CustomOriginConfig": {
                "HTTPPort": 80,
                "HTTPSPort": 443,
                "OriginProtocolPolicy": "http-only",
                "OriginSslProtocols": {"Quantity": 1, "Items": ["TLSv1.2"]},
            },
        }
        if origin_header_value:
            alb_origin["CustomHeaders"] = {
                "Quantity": 1,
                "Items": [
                    {
                        "HeaderName": CUSTOM_HEADER_NAME,
                        "HeaderValue": origin_header_value,
                    },
                ],
            }
        origin_items: List[Dict[str, Any]] = [alb_origin]

        cache_behaviors: Dict[str, Any] = {"Quantity": 0, "Items": []}
        oac_id: Optional[str] = None

        if s3_bucket_name:
            oac_id = self._create_cloudfront_oac()
            s3_domain = (
                f"{s3_bucket_name}.s3.amazonaws.com"
                if self.region == "us-east-1"
                else f"{s3_bucket_name}.s3.{self.region}.amazonaws.com"
            )
            origin_id_s3 = "openclaw-s3-docs-origin"
            origin_items.append({
                "Id": origin_id_s3,
                "DomainName": s3_domain,
                "OriginPath": "",
                "CustomHeaders": {"Quantity": 0},
                "OriginAccessControlId": oac_id,
                "S3OriginConfig": {"OriginAccessIdentity": ""},
                "ConnectionAttempts": 3,
                "ConnectionTimeout": 10,
                "OriginShield": {"Enabled": False},
            })
            cache_behaviors = {
                "Quantity": 1,
                "Items": [{
                    "PathPattern": "/docs/*",
                    "TargetOriginId": origin_id_s3,
                    "TrustedSigners": {"Enabled": False, "Quantity": 0},
                    "TrustedKeyGroups": {"Enabled": False, "Quantity": 0},
                    "ViewerProtocolPolicy": "redirect-to-https",
                    "AllowedMethods": {
                        "Quantity": 2,
                        "Items": ["GET", "HEAD"],
                        "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]},
                    },
                    "SmoothStreaming": False,
                    "Compress": True,
                    "LambdaFunctionAssociations": {"Quantity": 0},
                    "FunctionAssociations": {"Quantity": 0},
                    "FieldLevelEncryptionId": "",
                    "ForwardedValues": {
                        "QueryString": False,
                        "Cookies": {"Forward": "none"},
                        "Headers": {"Quantity": 0},
                        "QueryStringCacheKeys": {"Quantity": 0},
                    },
                    "MinTTL": 0,
                    "DefaultTTL": 86400,
                    "MaxTTL": 31536000,
                }],
            }
            logger.info("  S3 Origin 추가: /docs/* → s3://%s/docs/", s3_bucket_name)

        dist = self.cf.create_distribution(DistributionConfig={
            "CallerReference": caller_ref,
            "Comment": f"{self.project} CloudFront",
            "Enabled": True,
            "HttpVersion": "http2",
            "PriceClass": "PriceClass_All",
            "Origins": {
                "Quantity": len(origin_items),
                "Items": origin_items,
            },
            "DefaultCacheBehavior": {
                "TargetOriginId": origin_id_alb,
                "ViewerProtocolPolicy": "redirect-to-https",
                "AllowedMethods": {
                    "Quantity": 7,
                    "Items": ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"],
                    "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]},
                },
                "Compress": True,
                "ForwardedValues": {
                    "QueryString": True,
                    "Cookies": {"Forward": "all"},
                    "Headers": {"Quantity": 1, "Items": ["*"]},
                },
                "MinTTL": 0,
                "DefaultTTL": 0,
                "MaxTTL": 0,
            },
            "CacheBehaviors": cache_behaviors,
            "ViewerCertificate": {"CloudFrontDefaultCertificate": True},
        })["Distribution"]
        did = dist["Id"]
        dom = dist["DomainName"]
        logger.info("  CloudFront 생성: %s (%s)", dom, did)

        if s3_bucket_name:
            self._add_cloudfront_s3_bucket_policy(
                s3_bucket_name,
                f"arn:aws:cloudfront::{self.account_id}:distribution/{did}",
            )
            self._invalidate_cloudfront_cache(did, ["/docs/*"])

        return did, dom

    def _invalidate_cloudfront_cache(self, dist_id: str, paths: List[str]) -> None:
        """CloudFront 캐시 무효화 (기존 ALB 응답 캐시 제거)."""
        try:
            self.cf.create_invalidation(
                DistributionId=dist_id,
                InvalidationBatch={
                    "Paths": {"Quantity": len(paths), "Items": paths},
                    "CallerReference": f"{self.project}-{int(time.time())}",
                },
            )
            logger.info("  CloudFront 캐시 무효화: %s", paths)
        except ClientError as exc:
            logger.warning("  CloudFront 캐시 무효화 경고: %s", exc)

    def _create_cloudfront_oac(self) -> str:
        """CloudFront S3 Origin용 OAC 생성."""
        oac_name = f"oac-{self.project}-s3-docs"
        try:
            existing = self.cf.list_origin_access_controls().get("OriginAccessControlList", {})
            for item in existing.get("Items", []):
                if item.get("Name") == oac_name:
                    return item["Id"]
        except ClientError:
            pass

        resp = self.cf.create_origin_access_control(OriginAccessControlConfig={
            "Name": oac_name,
            "Description": f"OAC for {self.project} S3 docs",
            "SigningProtocol": "sigv4",
            "SigningBehavior": "always",
            "OriginAccessControlOriginType": "s3",
        })
        oac_id = resp["OriginAccessControl"]["Id"]
        logger.info("  CloudFront OAC 생성: %s", oac_id)
        return oac_id

    def _ensure_cloudfront_origin_header(
        self, dist_id: str, origin_header_value: str,
    ) -> None:
        """기존 CloudFront ALB Origin에 Custom Header 추가 (없을 경우)."""
        try:
            resp = self.cf.get_distribution_config(Id=dist_id)
            cfg = resp["DistributionConfig"]
            etag = resp["ETag"]
            updated = False
            for origin in cfg["Origins"].get("Items", []):
                if "CustomOriginConfig" not in origin:
                    continue
                headers = origin.setdefault("CustomHeaders", {"Quantity": 0, "Items": []})
                items = headers.get("Items", [])
                has_header = any(
                    h.get("HeaderName") == CUSTOM_HEADER_NAME for h in items
                )
                if not has_header:
                    items.append({
                        "HeaderName": CUSTOM_HEADER_NAME,
                        "HeaderValue": origin_header_value,
                    })
                    headers["Quantity"] = len(items)
                    headers["Items"] = items
                    updated = True
                    break
            if updated:
                self.cf.update_distribution(Id=dist_id, DistributionConfig=cfg, IfMatch=etag)
                logger.info("  CloudFront ALB Origin에 Custom Header 추가: %s", CUSTOM_HEADER_NAME)
        except ClientError as exc:
            logger.warning("  CloudFront Origin Header 추가 경고: %s", exc)

    def _ensure_cloudfront_s3_origin(self, dist_id: str, s3_bucket_name: str) -> None:
        """기존 CloudFront 배포에 S3 Origin 및 /docs/* Cache Behavior 추가."""
        try:
            resp = self.cf.get_distribution_config(Id=dist_id)
            cfg = resp["DistributionConfig"]
            etag = resp["ETag"]

            origin_id_s3 = "openclaw-s3-docs-origin"
            existing_origins = {o["Id"] for o in cfg["Origins"]["Items"]}
            if origin_id_s3 in existing_origins:
                logger.info("  CloudFront에 이미 S3 Origin 존재")
                self._invalidate_cloudfront_cache(dist_id, ["/docs/*"])
                return

            oac_id = self._create_cloudfront_oac()
            s3_domain = (
                f"{s3_bucket_name}.s3.amazonaws.com"
                if self.region == "us-east-1"
                else f"{s3_bucket_name}.s3.{self.region}.amazonaws.com"
            )
            cfg["Origins"]["Items"].append({
                "Id": origin_id_s3,
                "DomainName": s3_domain,
                "OriginPath": "",
                "CustomHeaders": {"Quantity": 0},
                "OriginAccessControlId": oac_id,
                "S3OriginConfig": {"OriginAccessIdentity": ""},
                "ConnectionAttempts": 3,
                "ConnectionTimeout": 10,
                "OriginShield": {"Enabled": False},
            })
            cfg["Origins"]["Quantity"] = len(cfg["Origins"]["Items"])

            docs_behavior = {
                "PathPattern": "/docs/*",
                "TargetOriginId": origin_id_s3,
                "TrustedSigners": {"Enabled": False, "Quantity": 0},
                "TrustedKeyGroups": {"Enabled": False, "Quantity": 0},
                "ViewerProtocolPolicy": "redirect-to-https",
                "AllowedMethods": {
                    "Quantity": 2,
                    "Items": ["GET", "HEAD"],
                    "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]},
                },
                "SmoothStreaming": False,
                "Compress": True,
                "LambdaFunctionAssociations": {"Quantity": 0},
                "FunctionAssociations": {"Quantity": 0},
                "FieldLevelEncryptionId": "",
                "ForwardedValues": {
                    "QueryString": False,
                    "Cookies": {"Forward": "none"},
                    "Headers": {"Quantity": 0},
                    "QueryStringCacheKeys": {"Quantity": 0},
                },
                "MinTTL": 0,
                "DefaultTTL": 86400,
                "MaxTTL": 31536000,
            }
            existing_behaviors = cfg["CacheBehaviors"].get("Items", [])
            if not any(b.get("PathPattern") == "/docs/*" for b in existing_behaviors):
                existing_behaviors.append(docs_behavior)
                cfg["CacheBehaviors"]["Items"] = existing_behaviors
                cfg["CacheBehaviors"]["Quantity"] = len(existing_behaviors)

            # S3 버킷 정책을 먼저 추가 (CloudFront가 Origin 검증 시 접근 가능해야 함)
            dist_arn = f"arn:aws:cloudfront::{self.account_id}:distribution/{dist_id}"
            self._add_cloudfront_s3_bucket_policy(s3_bucket_name, dist_arn)

            self.cf.update_distribution(Id=dist_id, DistributionConfig=cfg, IfMatch=etag)
            logger.info("  CloudFront 업데이트: S3 Origin /docs/* 추가")
            self._invalidate_cloudfront_cache(dist_id, ["/docs/*"])
        except ClientError as exc:
            logger.warning("  CloudFront S3 Origin 추가 경고: %s", exc)

    def _add_cloudfront_s3_bucket_policy(self, bucket_name: str, distribution_arn: str) -> None:
        """S3 버킷에 CloudFront OAC 접근을 허용하는 정책 추가."""
        cf_statement = {
            "Sid": "AllowCloudFrontServicePrincipal",
            "Effect": "Allow",
            "Principal": {"Service": "cloudfront.amazonaws.com"},
            "Action": "s3:GetObject",
            "Resource": f"arn:aws:s3:::{bucket_name}/*",
            "Condition": {"StringEquals": {"AWS:SourceArn": distribution_arn}},
        }
        try:
            try:
                current = self.s3.get_bucket_policy(Bucket=bucket_name)
                policy = json.loads(current["Policy"])
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "NoSuchBucketPolicy":
                    raise
                policy = {"Version": "2012-10-17", "Statement": []}

            statements = policy.get("Statement", [])
            if not any(s.get("Sid") == "AllowCloudFrontServicePrincipal" for s in statements):
                statements.append(cf_statement)
                policy["Statement"] = statements
                self.s3.put_bucket_policy(Bucket=bucket_name, Policy=json.dumps(policy))
                logger.info("  S3 버킷 정책 업데이트: CloudFront OAC 허용")
        except ClientError as exc:
            logger.warning("  S3 버킷 정책 업데이트 경고: %s", exc)

    # ==================================================================
    # Deployment Info
    # ==================================================================
    def _write_deployment_info(self, path: Path) -> None:
        o = self.out
        cf_url = f"https://{o['cloudfront_domain']}/" if o.get("cloudfront_domain") else "(비활성)"
        alb_url = f"http://{o['alb_dns']}/"

        md = textwrap.dedent(f"""\
            # OpenClaw AWS 배포 리소스 정보

            ## 생성 시각 (UTC)
            - {o["timestamp"]}

            ## 핵심 리소스

            | 항목 | 값 |
            |------|-----|
            | Region | {o["region"]} |
            | Account | {o["account_id"]} |
            | VPC | {o["vpc_id"]} ({o["vpc_cidr"]}) |
            | Public Subnets | {", ".join(o["public_subnets"])} |
            | Private Subnets | {", ".join(o["private_subnets"])} |
            | EC2 Instance | {o["instance_id"]} ({o["private_ip"]}) |
            | EC2 SG | {o["ec2_sg"]} |
            | ALB | {o["alb_dns"]} |
            | ALB SG | {o["alb_sg"]} |
            | Target Group | {o["tg_arn"]} |
            | Bedrock VPC Endpoint | {o["vpce_id"]} |
            | NAT Gateway | {o.get("nat_gateway_id") or "N/A"} |
            | IAM Role | {o["iam_role"]} |
            | Instance Profile | {o["instance_profile"]} |
            | CloudFront | {o.get("cloudfront_id") or "N/A"} ({o.get("cloudfront_domain") or "N/A"}) |
            | S3 Bucket (KB) | {o.get("s3_bucket") or "N/A"} |
            | Knowledge Base ID | {o.get("knowledge_base_id") or "N/A"} |
            | OpenSearch Endpoint | {o.get("opensearch_endpoint") or "N/A"} |

            ## 접속 URL
            - CloudFront (HTTPS): {cf_url}
            - ALB (HTTP): {alb_url}

            ## Gateway Token
            ```
            {o["gateway_token"]}
            ```

            ## Telegram
            - dmPolicy: {o["telegram_dm_policy"]}
            - allowFrom: {json.dumps(o["telegram_allow_from"])}
            - streamMode: {o["telegram_stream_mode"]}

            ## SSM 접속
            ```bash
            aws ssm start-session --target {o["instance_id"]} --region {o["region"]}
            ```

            ## 설치 로그 확인
            ```bash
            aws ssm send-command \\
              --document-name "AWS-RunShellScript" \\
              --instance-ids "{o["instance_id"]}" \\
              --parameters 'commands=["tail -100 /var/log/openclaw-install.log"]' \\
              --region {o["region"]}
            ```
        """)
        if o.get("s3_bucket"):
            md += textwrap.dedent(f"""

            ## Skills 수동 동기화 (pptx, retrieve 등)
            EC2에 skills가 복사되지 않은 경우, 로컬에서 S3로 업로드 후 아래 명령으로 동기화:
            ```bash
            # 1) 로컬에서 skills S3 업로드 (프로젝트 루트에서)
            aws s3 cp skills/ s3://{o["s3_bucket"]}/artifacts/skills/ --recursive --region {o["region"]}

            # 2) EC2에서 S3 → clawd/skills 복사
            aws ssm send-command \\
              --document-name "AWS-RunShellScript" \\
              --instance-ids "{o["instance_id"]}" \\
              --parameters 'commands=["aws s3 cp s3://{o["s3_bucket"]}/artifacts/skills/ /home/ec2-user/clawd/skills/ --recursive","chown -R ec2-user:ec2-user /home/ec2-user/clawd/skills","sudo systemctl restart openclaw-gateway.service"]' \\
              --region {o["region"]}
            ```
            """)
        md += textwrap.dedent("""

            ## 서비스 관리
            ```bash
            sudo systemctl start   openclaw-gateway.service
            sudo systemctl stop    openclaw-gateway.service
            sudo systemctl restart openclaw-gateway.service
            sudo systemctl status  openclaw-gateway.service
            sudo journalctl -u openclaw-gateway.service -f
            ```
        """)
        if o.get("knowledge_base_id") and o.get("s3_bucket"):
            docs_url = f"https://{o['cloudfront_domain']}/docs/" if o.get("cloudfront_domain") else "(CloudFront 비활성)"
            md += textwrap.dedent(f"""

            ## Knowledge Base 사용법
            - S3 버킷 `{o["s3_bucket"]}` 의 `docs/` 폴더에 문서 업로드
            - `aws s3 cp your-docs/ s3://{o["s3_bucket"]}/docs/ --recursive`
            - Bedrock Console → Knowledge Bases → Data source Sync 실행
            - **문서 공개 URL**: {docs_url} (CloudFront를 통해 S3 docs/ 제공)
            """)
            if o.get("cloudfront_id"):
                md += textwrap.dedent(f"""

            ## /docs/ 접속 실패 시 (캐시 무효화)
            문서 URL이 HTML(OpenClaw UI)을 반환하면 CloudFront 캐시 문제입니다. 캐시 무효화:
            ```bash
            aws cloudfront create-invalidation --distribution-id {o["cloudfront_id"]} --paths "/docs/*" --region {o["region"]}
            ```
            """)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(md, encoding="utf-8")
        logger.info("  %s 생성 완료", path)


# ===================================================================
# Application readiness check
# ===================================================================

def check_application_ready(
    domain: str,
    max_attempts: int = 120,
    wait_seconds: int = 10,
) -> bool:
    """CloudFront/ALB endpoint로 HTTP 요청을 보내 애플리케이션 준비 상태를 확인한다."""
    url = f"https://{domain}" if not domain.startswith("http") else domain
    logger.info("엔드포인트 접속 확인: %s", url)
    logger.info(
        "  최대 %d회 시도, %d초 간격 (최대 %d분)",
        max_attempts, wait_seconds, max_attempts * wait_seconds // 60,
    )

    start = time.time()
    last_log = start

    for attempt in range(1, max_attempts + 1):
        elapsed = time.time() - start
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                code = resp.getcode()
                if code == 200:
                    logger.info(
                        "✓ 애플리케이션 준비 완료! (HTTP %d, %d회차, %.1f분)",
                        code, attempt, elapsed / 60,
                    )
                    return True
        except urllib.error.HTTPError as exc:
            if exc.code in (502, 503, 504):
                now = time.time()
                if now - last_log >= 30 or attempt == 1:
                    logger.info(
                        "  배포 진행중... [%d/%d] HTTP %d (%.0f초 경과)",
                        attempt, max_attempts, exc.code, elapsed,
                    )
                    last_log = now
            else:
                logger.info(
                    "✓ 애플리케이션 응답 (HTTP %d, %d회차, %.1f분)",
                    exc.code, attempt, elapsed / 60,
                )
                return True
        except (urllib.error.URLError, OSError):
            now = time.time()
            if now - last_log >= 30 or attempt == 1:
                logger.info(
                    "  배포 진행중... [%d/%d] 연결 대기 (%.0f초 경과)",
                    attempt, max_attempts, elapsed,
                )
                last_log = now

        if attempt < max_attempts:
            time.sleep(wait_seconds)

    elapsed = time.time() - start
    logger.warning(
        "접속 확인 시간 초과 (%d초, %.1f분). 수동으로 확인하세요: %s",
        max_attempts * wait_seconds, elapsed / 60, url,
    )
    return False


# ===================================================================
# Fix CloudFront docs (S3 Origin)
# ===================================================================

def _run_fix_cloudfront_docs(
    region: str,
    project: str,
    deployment_info_path: Path,
) -> None:
    """기존 CloudFront에 S3 Origin 및 /docs/* Cache Behavior 추가."""
    import re

    installer = Installer(region=region, project=project)
    dist_id = None
    s3_bucket = None

    if deployment_info_path.exists():
        text = deployment_info_path.read_text("utf-8")
        m = re.search(r"\| CloudFront \| ([A-Z0-9]+) ", text)
        if m:
            dist_id = m.group(1)
        m = re.search(r"\| S3 Bucket \(KB\) \| ([^|]+) \|", text)
        if m:
            s3_bucket = m.group(1).strip()
        m = re.search(r"storage-for-[^\s|]+", text)
        if m and not s3_bucket:
            s3_bucket = m.group(0).strip()

    if not dist_id:
        existing = installer._find_existing_cloudfront()
        if existing:
            dist_id = existing[0]
    if not dist_id:
        logger.error("CloudFront Distribution ID를 찾을 수 없습니다. deployment-info.md를 확인하세요.")
        sys.exit(1)

    if not s3_bucket:
        s3_bucket = f"storage-for-{project}-{installer.account_id}-{region}"
    logger.info("CloudFront S3 Origin 추가: dist=%s, bucket=%s", dist_id, s3_bucket)
    installer._ensure_cloudfront_s3_origin(dist_id, s3_bucket)
    logger.info("완료. CloudFront 배포 반영까지 5-10분 소요될 수 있습니다.")


# ===================================================================
# CLI
# ===================================================================

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="OpenClaw AWS 자동 배포 (CloudFront → ALB → EC2 + Telegram)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            예시 (최소 인자):
              python installer.py --telegram-bot-token "123456:ABC..."

            모든 인프라(VPC/Subnet/IGW/NAT/SG/ALB/CloudFront)를 자동 생성합니다.
            기존 VPC가 있으면 재사용합니다.
        """),
    )

    parser.add_argument("--region", default=REGION, help=f"AWS 리전 (기본: {REGION})")
    parser.add_argument("--project-name", default=PROJECT_NAME, help=f"프로젝트 이름 (기본: {PROJECT_NAME})")
    parser.add_argument("--telegram-bot-token", help="Telegram Bot Token (미지정 시 대화형 입력)")
    parser.add_argument("--telegram-dm-policy", default="open", choices=["open", "pairing", "allowlist"])
    parser.add_argument("--telegram-allow-from", nargs="*", default=["*"], help='허용 사용자 (기본: "*")')
    parser.add_argument("--telegram-stream-mode", default="partial", choices=["partial", "full", "off"])
    parser.add_argument("--key-name", default=None, help="EC2 Key Pair (미지정 시 SSM 접속 전용)")
    parser.add_argument("--instance-type", default=INSTANCE_TYPE)
    parser.add_argument("--ami-id", default=AMI_ID)
    parser.add_argument("--volume-size", type=int, default=VOLUME_SIZE)
    parser.add_argument("--config-path", default=str(CONFIG_PATH), help="openclaw-config.json 경로")
    parser.add_argument("--deployment-info-path", default=str(DEPLOYMENT_INFO_PATH))
    parser.add_argument("--disable-cloudfront", action="store_true")
    parser.add_argument("--disable-knowledge-base", action="store_true", help="Knowledge Base 비활성화")
    parser.add_argument(
        "--fix-cloudfront-docs",
        action="store_true",
        help="기존 CloudFront에 S3 /docs/* Origin 추가 (배포 후 /docs/ 접속 실패 시 사용)",
    )

    args = parser.parse_args()

    # ---- fix-cloudfront-docs 전용 모드 ----
    if args.fix_cloudfront_docs:
        _run_fix_cloudfront_docs(
            region=args.region,
            project=args.project_name,
            deployment_info_path=Path(args.deployment_info_path),
        )
        return

    # ---- 시작 배너 ----
    installer = Installer(region=args.region, project=args.project_name)

    kb_steps = 0 if args.disable_knowledge_base else 5
    total_steps = (10 if not args.disable_cloudfront else 9) + kb_steps

    logger.info("")
    logger.info("%s", "=" * 60)
    logger.info("  OpenClaw AWS Infrastructure Deployment")
    logger.info("%s", "=" * 60)
    logger.info("")
    logger.info("  아키텍처: CloudFront → ALB → EC2 (Private Subnet)")
    logger.info("            EC2에 OpenClaw Gateway + Telegram Bot 설치")
    logger.info("            Bedrock API는 VPC Endpoint(PrivateLink) 사용")
    logger.info("")
    logger.info("  진행 순서 (총 %d단계):", total_steps)
    step_num = 1
    logger.info("    %d) VPC / Subnet / IGW / NAT Gateway 생성", step_num); step_num += 1
    if not args.disable_knowledge_base:
        logger.info("    %d) S3 Bucket 생성 (Knowledge Base용)", step_num); step_num += 1
        if SKILLS_PATH.exists() and SKILLS_PATH.is_dir():
            logger.info("    %d) Skills 폴더 S3 업로드", step_num); step_num += 1
        logger.info("    %d) Knowledge Base IAM Role 생성", step_num); step_num += 1
    logger.info("    %d) IAM Role + Instance Profile 생성", step_num); step_num += 1
    logger.info("    %d) Security Groups 생성 (EC2, ALB)", step_num); step_num += 1
    logger.info("    %d) Bedrock Runtime VPC Endpoint 생성", step_num); step_num += 1
    if not args.disable_knowledge_base:
        logger.info("    %d) OpenSearch Serverless Collection 생성", step_num); step_num += 1
        logger.info("    %d) OpenSearch Vector Index 생성", step_num); step_num += 1
        logger.info("    %d) Knowledge Base 생성", step_num); step_num += 1
    logger.info("    %d) EC2 UserData 렌더링 (OpenClaw 설정)", step_num); step_num += 1
    logger.info("    %d) EC2 인스턴스 생성 (Private Subnet)", step_num); step_num += 1
    logger.info("    %d) ALB + Target Group + Listener 생성", step_num); step_num += 1
    if not args.disable_cloudfront:
        logger.info("    %d) CloudFront Distribution 생성", step_num); step_num += 1
    logger.info("    %d) deployment-info.md 생성", step_num); step_num += 1
    logger.info("    %d) 엔드포인트 접속 확인", step_num)
    logger.info("")
    logger.info("  설정:")
    logger.info("    Project      : %s", args.project_name)
    logger.info("    Region       : %s", args.region)
    logger.info("    Account ID   : %s", installer.account_id)
    logger.info("    Instance Type: %s", args.instance_type)
    logger.info("    AMI          : %s", args.ami_id)
    logger.info("    Volume       : %s GB", args.volume_size)
    logger.info(
        "    CloudFront   : %s",
        "enabled" if not args.disable_cloudfront else "disabled",
    )
    logger.info(
        "    Knowledge Base: %s",
        "disabled" if args.disable_knowledge_base else "enabled (S3/OpenSearch/KB)",
    )
    logger.info("    Telegram     : policy=%s", args.telegram_dm_policy)
    logger.info("    Key Pair     : %s", args.key_name or "(없음 - SSM 접속 전용)")
    logger.info("")
    logger.info("%s", "=" * 60)
    
    bot_token = args.telegram_bot_token
    if not bot_token:
        logger.info("")
        logger.info("%s", "-" * 60)
        logger.info("  Telegram Bot Token이 필요합니다.")
        logger.info("")
        logger.info("  Token 발급 방법:")
        logger.info("    1. Telegram에서 @BotFather 와 대화 시작")
        logger.info("    2. /newbot 명령 입력")
        logger.info("    3. Bot 이름 입력 (예: OpenClaw Assistant)")
        logger.info("    4. Bot username 입력 (예: openclaw_assistant_bot)")
        logger.info("    5. BotFather가 제공하는 Token을 복사")
        logger.info("")
        logger.info("  자세한 내용:")
        logger.info("    https://github.com/kyopark2014/openclaw/blob/main/README.md#telegram-token")
        logger.info("%s", "-" * 60)
        bot_token = input("\nTelegram Bot Token을 입력하세요: ").strip()
        if not bot_token:
            logger.error("Telegram Bot Token이 필요합니다.")
            sys.exit(1)

    start_time = time.time()

    try:
        outputs = installer.run(
            telegram_bot_token=bot_token,
            telegram_dm_policy=args.telegram_dm_policy,
            telegram_allow_from=args.telegram_allow_from,
            telegram_stream_mode=args.telegram_stream_mode,
            key_name=args.key_name,
            instance_type=args.instance_type,
            ami_id=args.ami_id,
            volume_size=args.volume_size,
            enable_cloudfront=not args.disable_cloudfront,
            enable_knowledge_base=not args.disable_knowledge_base,
            config_path=Path(args.config_path),
            deployment_info_path=Path(args.deployment_info_path),
        )
    except Exception as exc:
        elapsed = time.time() - start_time
        logger.error("")
        logger.error("=" * 60)
        logger.error("  Deployment Failed!")
        logger.error("=" * 60)
        logger.error("  Error: %s", exc)
        logger.error("  Elapsed: %.2f minutes", elapsed / 60)
        logger.error("=" * 60)
        import traceback
        logger.error(traceback.format_exc())
        sys.exit(1)

    elapsed = time.time() - start_time
    cf_domain = outputs.get("cloudfront_domain")
    alb_dns = outputs.get("alb_dns")

    # ---- 접속 확인 ----
    total_steps = 10 if not args.disable_cloudfront else 9
    check_url = f"https://{cf_domain}" if cf_domain else f"http://{alb_dns}"
    logger.info("")
    logger.info("%d) 엔드포인트 접속 확인  [%d/%d]", total_steps, total_steps, total_steps)
    check_application_ready(check_url)

    # ---- 최종 요약 ----
    elapsed = time.time() - start_time
    logger.info("")
    logger.info("%s", "=" * 60)
    logger.info("  Infrastructure Deployment Completed Successfully!")
    logger.info("%s", "=" * 60)
    logger.info("")
    logger.info("  Summary:")
    logger.info("    VPC              : %s (%s)", outputs["vpc_id"], outputs["vpc_cidr"])
    logger.info("    Public Subnets   : %s", ", ".join(outputs["public_subnets"]))
    logger.info("    Private Subnets  : %s", ", ".join(outputs["private_subnets"]))
    logger.info("    EC2 Instance     : %s (%s)", outputs["instance_id"], outputs["private_ip"])
    logger.info("    ALB DNS          : http://%s", alb_dns)
    if cf_domain:
        logger.info("    CloudFront       : https://%s", cf_domain)
    logger.info("    NAT Gateway      : %s", outputs.get("nat_gateway_id") or "N/A")
    logger.info("    Bedrock Endpoint : %s", outputs["vpce_id"])
    if outputs.get("knowledge_base_id"):
        logger.info("    S3 Bucket (KB)   : %s", outputs.get("s3_bucket") or "N/A")
        logger.info("    Knowledge Base  : %s", outputs["knowledge_base_id"])
    logger.info("")
    logger.info("  Total deployment time: %.2f minutes", elapsed / 60)
    logger.info("")
    logger.info("%s", "=" * 60)

    # ---- 접속 URL 강조 ----
    logger.info("")
    logger.info("%s", "=" * 60)
    logger.info("  IMPORTANT: Access URL")
    logger.info("%s", "=" * 60)
    if cf_domain:
        logger.info("")
        logger.info("    CloudFront (HTTPS) : https://%s", cf_domain)
    logger.info("    ALB (HTTP)         : http://%s", alb_dns)
    logger.info("")
    logger.info("    Gateway Token: %s", outputs["gateway_token"])
    logger.info("")
    if cf_domain:
        logger.info("  Note: CloudFront 배포가 완전히 완료되기까지 10-15분이 소요될 수 있습니다.")
    logger.info("  Note: EC2 UserData가 OpenClaw를 설치하고 서비스를 시작합니다.")
    logger.info("  Note: 배포 정보 파일: %s", args.deployment_info_path)
    logger.info("%s", "=" * 60)
    logger.info("")


if __name__ == "__main__":
    main()

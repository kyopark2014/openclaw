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
DEPLOYMENT_INFO_PATH = Path("deployment-info.md")

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
        config_path: Path = CONFIG_PATH,
        deployment_info_path: Path = DEPLOYMENT_INFO_PATH,
    ) -> Dict[str, Any]:
        if telegram_allow_from is None:
            telegram_allow_from = ["*"]

        total = 10 if enable_cloudfront else 9
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

        _step("IAM Role + Instance Profile")
        role_name, profile_name, profile_arn = self._ensure_iam()

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
        alb_arn, alb_dns, tg_arn = self._create_alb(
            vpc_id=vpc_id,
            public_subnets=public_subnets,
            alb_sg=alb_sg,
            instance_id=inst_id,
        )

        cf_id, cf_domain = None, None
        if enable_cloudfront:
            _step("CloudFront Distribution")
            cf_id, cf_domain = self._create_cloudfront(alb_dns)

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
    def _ensure_iam(self) -> tuple[str, str, str]:
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
            "Statement": [{
                "Effect": "Allow",
                "Action": [
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                    "bedrock:ListFoundationModels",
                    "bedrock:GetFoundationModel",
                    "bedrock:GetInferenceProfile",
                ],
                "Resource": "*",
            }],
        }

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

        self.iam.update_assume_role_policy(RoleName=role_name, PolicyDocument=json.dumps(trust))
        self.iam.put_role_policy(
            RoleName=role_name, PolicyName="BedrockAccess",
            PolicyDocument=json.dumps(bedrock_policy),
        )
        try:
            self.iam.attach_role_policy(
                RoleName=role_name,
                PolicyArn="arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
            )
        except ClientError:
            pass

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
        existing_roles = {r["RoleName"] for r in profile["Roles"]}
        if role_name not in existing_roles:
            for old_role in existing_roles:
                logger.info("  기존 Role 제거: %s (from %s)", old_role, profile_name)
                self.iam.remove_role_from_instance_profile(
                    InstanceProfileName=profile_name, RoleName=old_role,
                )
            self.iam.add_role_to_instance_profile(
                InstanceProfileName=profile_name, RoleName=role_name,
            )

        # IAM eventual consistency: Role이 연결된 상태로 EC2에서 사용 가능해질 때까지 폴링
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
    ) -> str:
        config = self._build_openclaw_config(
            gateway_token, vpc_cidr,
            telegram_bot_token, telegram_dm_policy,
            telegram_allow_from, telegram_stream_mode,
            config_path,
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
            "mkdir -p /home/ec2-user/.openclaw /home/ec2-user/clawd",
            "cat > /home/ec2-user/.openclaw/openclaw.json <<'OCJSON'",
            config_json,
            "OCJSON",
            "",
            f'echo "{gateway_token}" > /home/ec2-user/openclaw-token.txt',
            "chown -R ec2-user:ec2-user /home/ec2-user/.openclaw /home/ec2-user/clawd /home/ec2-user/openclaw-token.txt",
            "",
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
        ]
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
                            }
                        ],
                    }
                }
            }

        ag = config.setdefault("agents", {}).setdefault("defaults", {})
        ag["workspace"] = "/home/ec2-user/clawd"
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
        tg["streamMode"] = telegram_stream_mode

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
        self, vpc_id: str, public_subnets: List[str], alb_sg: str, instance_id: str,
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
        self.elbv2.create_listener(
            LoadBalancerArn=alb_arn,
            Protocol="HTTP",
            Port=80,
            DefaultActions=[{"Type": "forward", "TargetGroupArn": tg_arn}],
        )
        return alb_arn, alb_dns, tg_arn

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

    def _create_cloudfront(self, alb_dns: str) -> tuple[str, str]:
        existing = self._find_existing_cloudfront()
        if existing:
            did, dom = existing
            logger.info("  기존 CloudFront 발견: %s (%s)", dom, did)
            return did, dom

        caller_ref = f"{self.project}-{int(time.time())}"
        origin_id = "openclaw-alb-origin"
        dist = self.cf.create_distribution(DistributionConfig={
            "CallerReference": caller_ref,
            "Comment": f"{self.project} CloudFront",
            "Enabled": True,
            "HttpVersion": "http2",
            "PriceClass": "PriceClass_All",
            "Origins": {
                "Quantity": 1,
                "Items": [{
                    "Id": origin_id,
                    "DomainName": alb_dns,
                    "CustomOriginConfig": {
                        "HTTPPort": 80,
                        "HTTPSPort": 443,
                        "OriginProtocolPolicy": "http-only",
                        "OriginSslProtocols": {"Quantity": 1, "Items": ["TLSv1.2"]},
                    },
                }],
            },
            "DefaultCacheBehavior": {
                "TargetOriginId": origin_id,
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
            "ViewerCertificate": {"CloudFrontDefaultCertificate": True},
        })["Distribution"]
        did = dist["Id"]
        dom = dist["DomainName"]
        logger.info("  CloudFront 생성: %s (%s)", dom, did)
        return did, dom

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

            ## 서비스 관리
            ```bash
            sudo systemctl start   openclaw-gateway.service
            sudo systemctl stop    openclaw-gateway.service
            sudo systemctl restart openclaw-gateway.service
            sudo systemctl status  openclaw-gateway.service
            sudo journalctl -u openclaw-gateway.service -f
            ```
        """)
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

    args = parser.parse_args()

    # ---- 시작 배너 ----
    installer = Installer(region=args.region, project=args.project_name)

    total_steps = 10 if not args.disable_cloudfront else 9

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
    logger.info("    1) VPC / Subnet / IGW / NAT Gateway 생성")
    logger.info("    2) IAM Role + Instance Profile 생성")
    logger.info("    3) Security Groups 생성 (EC2, ALB)")
    logger.info("    4) Bedrock Runtime VPC Endpoint 생성")
    logger.info("    5) EC2 UserData 렌더링 (OpenClaw 설정)")
    logger.info("    6) EC2 인스턴스 생성 (Private Subnet)")
    logger.info("    7) ALB + Target Group + Listener 생성")
    if not args.disable_cloudfront:
        logger.info("    8) CloudFront Distribution 생성")
        logger.info("    9) deployment-info.md 생성")
        logger.info("   10) 엔드포인트 접속 확인")
    else:
        logger.info("    8) deployment-info.md 생성")
        logger.info("    9) 엔드포인트 접속 확인")
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

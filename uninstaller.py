#!/usr/bin/env python3
"""
OpenClaw AWS one-shot uninstaller.

installer.py 가 생성한 리소스를 역순으로 정리/삭제한다.
기본 아키텍처:
CloudFront -> ALB -> EC2 -> (VPC 내부 리소스) -> IAM
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from typing import Dict, List

import boto3
from botocore.exceptions import ClientError, NoCredentialsError


PROJECT_NAME = "openclaw"
REGION = "us-west-2"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


class Uninstaller:
    def __init__(self, *, region: str = REGION, project: str = PROJECT_NAME):
        self.region = region
        self.project = project
        self.session = boto3.Session(region_name=region)
        self.ec2 = self.session.client("ec2")
        self.elbv2 = self.session.client("elbv2")
        self.cf = self.session.client("cloudfront")
        self.iam = self.session.client("iam")
        self.sts = self.session.client("sts")
        self._step = 0

        try:
            self.account_id = self.sts.get_caller_identity()["Account"]
        except NoCredentialsError:
            logger.error("AWS 자격 증명을 찾을 수 없습니다.")
            sys.exit(1)

    def _next_step(self, desc: str) -> None:
        self._step += 1
        logger.info("%d) %s", self._step, desc)

    def run(self) -> None:
        logger.info("=" * 60)
        logger.info("OpenClaw AWS Infrastructure Uninstall")
        logger.info("=" * 60)
        logger.info("Project  : %s", self.project)
        logger.info("Region   : %s", self.region)
        logger.info("Account  : %s", self.account_id)
        logger.info("=" * 60)

        start = time.time()
        self._step = 0

        self.delete_cloudfront()
        self.delete_alb_and_target_group()
        self.terminate_ec2_instances()
        self.delete_vpcs_and_networking()
        self.delete_iam()
        self.delete_cloudfront_retry()

        elapsed = (time.time() - start) / 60
        logger.info("=" * 60)
        logger.info("Uninstall completed. (%.2f minutes)", elapsed)
        logger.info("=" * 60)

    # ------------------------------------------------------------------
    # CloudFront
    # ------------------------------------------------------------------
    def _find_cloudfront_distributions(self) -> List[str]:
        target_comment = f"{self.project} CloudFront"
        dist_ids: List[str] = []
        paginator = self.cf.get_paginator("list_distributions")
        for page in paginator.paginate():
            dist_list = page.get("DistributionList", {})
            for item in dist_list.get("Items", []):
                if item.get("Comment") == target_comment:
                    dist_ids.append(item["Id"])
        return dist_ids

    def _wait_cf_deployed(self, dist_id: str, timeout_sec: int = 900) -> bool:
        waited = 0
        while waited < timeout_sec:
            resp = self.cf.get_distribution(Id=dist_id)
            status = resp["Distribution"]["Status"]
            if status == "Deployed":
                return True
            logger.info(
                "  CloudFront 상태 대기중: %s (status=%s, %ds/%ds)",
                dist_id,
                status,
                waited,
                timeout_sec,
            )
            time.sleep(15)
            waited += 15
        return False

    def delete_cloudfront(self) -> None:
        self._next_step("CloudFront 배포 삭제")
        dist_ids = self._find_cloudfront_distributions()
        if not dist_ids:
            logger.info("  삭제 대상 CloudFront 없음")
            return

        for dist_id in dist_ids:
            try:
                cfg_resp = self.cf.get_distribution_config(Id=dist_id)
                cfg = cfg_resp["DistributionConfig"]
                etag = cfg_resp["ETag"]

                if cfg.get("Enabled", True):
                    cfg["Enabled"] = False
                    self.cf.update_distribution(
                        Id=dist_id,
                        DistributionConfig=cfg,
                        IfMatch=etag,
                    )
                    logger.info("  CloudFront 비활성화: %s", dist_id)

                    if not self._wait_cf_deployed(dist_id):
                        logger.warning("  비활성화 전파 대기 타임아웃: %s", dist_id)
                        continue

                    cfg_resp = self.cf.get_distribution_config(Id=dist_id)
                    etag = cfg_resp["ETag"]

                self.cf.delete_distribution(Id=dist_id, IfMatch=etag)
                logger.info("  CloudFront 삭제 요청: %s", dist_id)
            except ClientError as exc:
                code = exc.response["Error"]["Code"]
                if code in ("NoSuchDistribution", "DistributionNotDisabled"):
                    logger.info("  CloudFront 건너뜀(%s): %s", code, dist_id)
                else:
                    logger.warning("  CloudFront 삭제 실패 %s: %s", dist_id, exc)

    # ------------------------------------------------------------------
    # ALB / TG
    # ------------------------------------------------------------------
    def delete_alb_and_target_group(self) -> None:
        self._next_step("ALB / Target Group 삭제")
        alb_name = f"alb-{self.project}"[:32]
        tg_name = f"tg-{self.project}"[:32]

        alb_arn = None
        try:
            resp = self.elbv2.describe_load_balancers(Names=[alb_name])
            if resp.get("LoadBalancers"):
                alb_arn = resp["LoadBalancers"][0]["LoadBalancerArn"]
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "LoadBalancerNotFound":
                logger.warning("  ALB 조회 실패: %s", exc)

        if alb_arn:
            try:
                listeners = self.elbv2.describe_listeners(LoadBalancerArn=alb_arn).get("Listeners", [])
                for listener in listeners:
                    self.elbv2.delete_listener(ListenerArn=listener["ListenerArn"])
                    logger.info("  Listener 삭제: %s", listener["ListenerArn"])
            except ClientError as exc:
                logger.warning("  Listener 삭제 중 경고: %s", exc)

            try:
                self.elbv2.delete_load_balancer(LoadBalancerArn=alb_arn)
                logger.info("  ALB 삭제 요청: %s", alb_name)
                waiter = self.elbv2.get_waiter("load_balancers_deleted")
                waiter.wait(LoadBalancerArns=[alb_arn], WaiterConfig={"Delay": 15, "MaxAttempts": 40})
            except ClientError as exc:
                logger.warning("  ALB 삭제 실패: %s", exc)
            except Exception as exc:
                logger.warning("  ALB 삭제 대기 중 경고: %s", exc)
        else:
            logger.info("  삭제 대상 ALB 없음")

        try:
            tgs = self.elbv2.describe_target_groups(Names=[tg_name]).get("TargetGroups", [])
            for tg in tgs:
                self.elbv2.delete_target_group(TargetGroupArn=tg["TargetGroupArn"])
                logger.info("  Target Group 삭제: %s", tg["TargetGroupName"])
        except ClientError as exc:
            if exc.response["Error"]["Code"] not in ("TargetGroupNotFound", "ResourceInUse"):
                logger.warning("  Target Group 삭제 실패: %s", exc)

    # ------------------------------------------------------------------
    # EC2
    # ------------------------------------------------------------------
    def terminate_ec2_instances(self) -> None:
        self._next_step("EC2 인스턴스 종료")
        resp = self.ec2.describe_instances(
            Filters=[
                {"Name": "tag:Name", "Values": [self.project]},
                {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]},
            ]
        )
        instance_ids: List[str] = []
        for reservation in resp.get("Reservations", []):
            for inst in reservation.get("Instances", []):
                instance_ids.append(inst["InstanceId"])

        if not instance_ids:
            logger.info("  삭제 대상 EC2 없음")
            return

        self.ec2.terminate_instances(InstanceIds=instance_ids)
        logger.info("  종료 요청: %s", ", ".join(instance_ids))
        waiter = self.ec2.get_waiter("instance_terminated")
        waiter.wait(InstanceIds=instance_ids)
        logger.info("  EC2 종료 완료")

    # ------------------------------------------------------------------
    # VPC / Network
    # ------------------------------------------------------------------
    def delete_vpcs_and_networking(self) -> None:
        self._next_step("VPC 및 네트워크 리소스 삭제")
        vpc_name = f"vpc-for-{self.project}"
        vpcs = self.ec2.describe_vpcs(
            Filters=[{"Name": "tag:Name", "Values": [vpc_name]}]
        ).get("Vpcs", [])

        if not vpcs:
            logger.info("  삭제 대상 VPC 없음 (%s)", vpc_name)
            return

        for vpc in vpcs:
            self._delete_single_vpc(vpc["VpcId"])

    def _delete_single_vpc(self, vpc_id: str) -> None:
        logger.info("  VPC 삭제 시작: %s", vpc_id)

        # 1) VPC Endpoint
        try:
            eps = self.ec2.describe_vpc_endpoints(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]).get("VpcEndpoints", [])
            if eps:
                ep_ids = [e["VpcEndpointId"] for e in eps if e["State"] != "deleted"]
                if ep_ids:
                    self.ec2.delete_vpc_endpoints(VpcEndpointIds=ep_ids)
                    logger.info("    VPC Endpoint 삭제 요청: %s", ", ".join(ep_ids))
                    self._wait_for_vpc_endpoints_deleted(vpc_id)
        except ClientError as exc:
            logger.warning("    VPC Endpoint 삭제 경고: %s", exc)

        # 2) NAT Gateway + 관련 route 제거
        nat_eip_alloc_ids: List[str] = []
        try:
            nats = self.ec2.describe_nat_gateways(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]).get("NatGateways", [])
            for nat in nats:
                nat_id = nat["NatGatewayId"]
                if nat["State"] in ("deleted", "deleting"):
                    continue

                for addr in nat.get("NatGatewayAddresses", []):
                    alloc = addr.get("AllocationId")
                    if alloc:
                        nat_eip_alloc_ids.append(alloc)

                # NAT를 참조하는 route 삭제
                rts = self.ec2.describe_route_tables(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]).get("RouteTables", [])
                for rt in rts:
                    for route in rt.get("Routes", []):
                        if route.get("NatGatewayId") == nat_id and route.get("DestinationCidrBlock"):
                            try:
                                self.ec2.delete_route(
                                    RouteTableId=rt["RouteTableId"],
                                    DestinationCidrBlock=route["DestinationCidrBlock"],
                                )
                            except ClientError:
                                pass

                self.ec2.delete_nat_gateway(NatGatewayId=nat_id)
                logger.info("    NAT 삭제 요청: %s", nat_id)
        except ClientError as exc:
            logger.warning("    NAT 삭제 경고: %s", exc)

        # NAT 비동기 삭제 대기
        self._wait_for_nat_deleted(vpc_id)

        # 3) ENI(available) 정리
        try:
            enis = self.ec2.describe_network_interfaces(
                Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
            ).get("NetworkInterfaces", [])
            for eni in enis:
                if eni.get("Status") == "available":
                    try:
                        self.ec2.delete_network_interface(NetworkInterfaceId=eni["NetworkInterfaceId"])
                    except ClientError as exc:
                        logger.warning("    ENI 삭제 경고(%s): %s", eni["NetworkInterfaceId"], exc)
        except ClientError as exc:
            logger.warning("    ENI 조회 경고: %s", exc)

        # 4) Subnet 삭제 (비동기 전파 지연을 고려해 재시도)
        self._delete_subnets_with_retry(vpc_id, max_rounds=6, wait_sec=10)

        # 6) Route Table 삭제 (main 제외)
        try:
            rts = self.ec2.describe_route_tables(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]).get("RouteTables", [])
            for rt in rts:
                is_main = any(a.get("Main") for a in rt.get("Associations", []))
                if is_main:
                    continue
                for assoc in rt.get("Associations", []):
                    assoc_id = assoc.get("RouteTableAssociationId")
                    if assoc_id and not assoc.get("Main"):
                        try:
                            self.ec2.disassociate_route_table(AssociationId=assoc_id)
                        except ClientError:
                            pass
                try:
                    self.ec2.delete_route_table(RouteTableId=rt["RouteTableId"])
                    logger.info("    Route Table 삭제: %s", rt["RouteTableId"])
                except ClientError:
                    pass
        except ClientError as exc:
            logger.warning("    Route Table 삭제 경고: %s", exc)

        # 7) IGW 분리/삭제
        try:
            igws = self.ec2.describe_internet_gateways(
                Filters=[{"Name": "attachment.vpc-id", "Values": [vpc_id]}]
            ).get("InternetGateways", [])
            for igw in igws:
                igw_id = igw["InternetGatewayId"]
                try:
                    self.ec2.detach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)
                except ClientError:
                    pass
                try:
                    self.ec2.delete_internet_gateway(InternetGatewayId=igw_id)
                    logger.info("    IGW 삭제: %s", igw_id)
                except ClientError as exc:
                    logger.warning("    IGW 삭제 경고(%s): %s", igw_id, exc)
        except ClientError as exc:
            logger.warning("    IGW 조회 경고: %s", exc)

        # 8) NAT에 연결되었던 EIP 릴리즈
        for alloc_id in nat_eip_alloc_ids:
            try:
                self.ec2.release_address(AllocationId=alloc_id)
                logger.info("    EIP 릴리즈: %s", alloc_id)
            except ClientError as exc:
                logger.warning("    EIP 릴리즈 경고(%s): %s", alloc_id, exc)

        # 8.5) Security Group 정리 (Subnet/ENI 정리 이후 재시도)
        self._delete_security_groups_with_retry(vpc_id, max_rounds=6, wait_sec=10)

        # 9) VPC 삭제 (재시도)
        for attempt in range(5):
            try:
                self.ec2.delete_vpc(VpcId=vpc_id)
                logger.info("  VPC 삭제 완료: %s", vpc_id)
                return
            except ClientError as exc:
                code = exc.response["Error"]["Code"]
                if code == "InvalidVpcID.NotFound":
                    logger.info("  VPC 이미 삭제됨: %s", vpc_id)
                    return
                if code == "DependencyViolation" and attempt < 4:
                    wait_sec = 15 * (attempt + 1)
                    self._log_remaining_vpc_dependencies(vpc_id)
                    logger.info("  VPC 의존성 대기 후 재시도(%ds): %s", wait_sec, vpc_id)
                    time.sleep(wait_sec)
                    continue
                self._log_remaining_vpc_dependencies(vpc_id)
                logger.warning("  VPC 삭제 실패: %s (%s)", vpc_id, exc)
                return

    def _wait_for_vpc_endpoints_deleted(self, vpc_id: str, timeout_sec: int = 180) -> None:
        waited = 0
        while waited < timeout_sec:
            eps = self.ec2.describe_vpc_endpoints(
                Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
            ).get("VpcEndpoints", [])
            alive = [e["VpcEndpointId"] for e in eps if e.get("State") not in ("deleted", "failed")]
            if not alive:
                return
            time.sleep(10)
            waited += 10
        logger.warning("    VPC Endpoint 삭제 대기 타임아웃: %s", vpc_id)

    def _wait_for_nat_deleted(self, vpc_id: str, timeout_sec: int = 480) -> None:
        waited = 0
        while waited < timeout_sec:
            nats = self.ec2.describe_nat_gateways(
                Filter=[{"Name": "vpc-id", "Values": [vpc_id]}]
            ).get("NatGateways", [])
            alive = [n["NatGatewayId"] for n in nats if n.get("State") not in ("deleted", "failed")]
            if not alive:
                return
            time.sleep(15)
            waited += 15
        logger.warning("    NAT 삭제 대기 타임아웃: %s", vpc_id)

    def _delete_subnets_with_retry(self, vpc_id: str, max_rounds: int = 6, wait_sec: int = 10) -> None:
        for round_idx in range(max_rounds):
            subs = self.ec2.describe_subnets(
                Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
            ).get("Subnets", [])
            if not subs:
                return
            blocked = 0
            for subnet in subs:
                subnet_id = subnet["SubnetId"]
                try:
                    self.ec2.delete_subnet(SubnetId=subnet_id)
                    logger.info("    Subnet 삭제: %s", subnet_id)
                except ClientError as exc:
                    blocked += 1
                    logger.info("    Subnet 삭제 보류(%s): %s", subnet_id, exc.response["Error"]["Code"])
            if blocked == 0:
                return
            if round_idx < max_rounds - 1:
                time.sleep(wait_sec)

    def _delete_security_groups_with_retry(self, vpc_id: str, max_rounds: int = 6, wait_sec: int = 10) -> None:
        for round_idx in range(max_rounds):
            sgs = self.ec2.describe_security_groups(
                Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
            ).get("SecurityGroups", [])
            custom_sgs = [sg for sg in sgs if sg.get("GroupName") != "default"]
            if not custom_sgs:
                return
            blocked = 0
            for sg in custom_sgs:
                sg_id = sg["GroupId"]
                try:
                    if sg.get("IpPermissions"):
                        self.ec2.revoke_security_group_ingress(
                            GroupId=sg_id,
                            IpPermissions=sg["IpPermissions"],
                        )
                except ClientError:
                    pass
                try:
                    egress = [
                        r for r in sg.get("IpPermissionsEgress", [])
                        if not (
                            r.get("IpProtocol") == "-1"
                            and len(r.get("IpRanges", [])) == 1
                            and r["IpRanges"][0].get("CidrIp") == "0.0.0.0/0"
                        )
                    ]
                    if egress:
                        self.ec2.revoke_security_group_egress(GroupId=sg_id, IpPermissions=egress)
                except ClientError:
                    pass
                try:
                    self.ec2.delete_security_group(GroupId=sg_id)
                    logger.info("    SG 삭제: %s", sg_id)
                except ClientError as exc:
                    blocked += 1
                    logger.info("    SG 삭제 보류(%s): %s", sg_id, exc.response["Error"]["Code"])
            if blocked == 0:
                return
            if round_idx < max_rounds - 1:
                time.sleep(wait_sec)

    def _log_remaining_vpc_dependencies(self, vpc_id: str) -> None:
        try:
            subs = self.ec2.describe_subnets(
                Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
            ).get("Subnets", [])
            if subs:
                logger.warning("    남은 Subnet: %s", ", ".join(s["SubnetId"] for s in subs))

            sgs = self.ec2.describe_security_groups(
                Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
            ).get("SecurityGroups", [])
            custom_sgs = [sg["GroupId"] for sg in sgs if sg.get("GroupName") != "default"]
            if custom_sgs:
                logger.warning("    남은 SG: %s", ", ".join(custom_sgs))

            enis = self.ec2.describe_network_interfaces(
                Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
            ).get("NetworkInterfaces", [])
            if enis:
                logger.warning("    남은 ENI: %s", ", ".join(eni["NetworkInterfaceId"] for eni in enis))

            eps = self.ec2.describe_vpc_endpoints(
                Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
            ).get("VpcEndpoints", [])
            alive_eps = [ep["VpcEndpointId"] for ep in eps if ep.get("State") not in ("deleted", "failed")]
            if alive_eps:
                logger.warning("    남은 VPC Endpoint: %s", ", ".join(alive_eps))
        except ClientError as exc:
            logger.warning("    VPC 의존성 조회 실패: %s", exc)

    # ------------------------------------------------------------------
    # IAM
    # ------------------------------------------------------------------
    def delete_iam(self) -> None:
        self._next_step("IAM Role / Instance Profile 삭제")
        role_name = f"{self.project}-bedrock-role"
        profile_name = f"{self.project}-bedrock-profile"

        # Instance profile에서 role 제거 후 profile 삭제
        try:
            profile = self.iam.get_instance_profile(InstanceProfileName=profile_name)["InstanceProfile"]
            for role in profile.get("Roles", []):
                try:
                    self.iam.remove_role_from_instance_profile(
                        InstanceProfileName=profile_name,
                        RoleName=role["RoleName"],
                    )
                except ClientError:
                    pass
            self.iam.delete_instance_profile(InstanceProfileName=profile_name)
            logger.info("  Instance Profile 삭제: %s", profile_name)
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "NoSuchEntity":
                logger.warning("  Instance Profile 삭제 경고: %s", exc)

        # Role 정책 분리/삭제 후 role 삭제
        try:
            attached = self.iam.list_attached_role_policies(RoleName=role_name).get("AttachedPolicies", [])
            for p in attached:
                try:
                    self.iam.detach_role_policy(RoleName=role_name, PolicyArn=p["PolicyArn"])
                except ClientError:
                    pass

            inline = self.iam.list_role_policies(RoleName=role_name).get("PolicyNames", [])
            for p_name in inline:
                try:
                    self.iam.delete_role_policy(RoleName=role_name, PolicyName=p_name)
                except ClientError:
                    pass

            self.iam.delete_role(RoleName=role_name)
            logger.info("  IAM Role 삭제: %s", role_name)
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "NoSuchEntity":
                logger.warning("  IAM Role 삭제 경고: %s", exc)

    def delete_cloudfront_retry(self) -> None:
        """CloudFront 비활성화 전파 지연을 고려한 최종 정리."""
        dist_ids = self._find_cloudfront_distributions()
        if not dist_ids:
            return
        self._next_step("CloudFront 최종 정리 (재시도)")
        for dist_id in dist_ids:
            try:
                cfg_resp = self.cf.get_distribution_config(Id=dist_id)
                etag = cfg_resp["ETag"]
                self.cf.delete_distribution(Id=dist_id, IfMatch=etag)
                logger.info("  CloudFront 삭제 완료: %s", dist_id)
            except ClientError as exc:
                code = exc.response["Error"]["Code"]
                if code in ("NoSuchDistribution",):
                    logger.info("  CloudFront 이미 삭제됨: %s", dist_id)
                elif code == "DistributionNotDisabled":
                    logger.info("  CloudFront 비활성화 대기중: %s (수동 삭제 필요)", dist_id)
                else:
                    logger.warning("  CloudFront 삭제 실패 %s: %s", dist_id, exc)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="OpenClaw AWS 인프라 삭제 (installer.py 역정리)"
    )
    parser.add_argument("--region", default=REGION, help=f"AWS 리전 (기본: {REGION})")
    parser.add_argument("--project-name", default=PROJECT_NAME, help=f"프로젝트 이름 (기본: {PROJECT_NAME})")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="확인 프롬프트 없이 즉시 삭제",
    )
    args = parser.parse_args()

    if not args.yes:
        print("")
        print("=" * 60)
        print("WARNING: installer.py가 생성한 AWS 리소스를 삭제합니다.")
        print("=" * 60)
        answer = input("계속하시겠습니까? (yes/no): ").strip().lower()
        if answer != "yes":
            print("취소되었습니다.")
            return

    uninstaller = Uninstaller(region=args.region, project=args.project_name)
    uninstaller.run()


if __name__ == "__main__":
    main()

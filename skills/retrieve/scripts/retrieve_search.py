#!/usr/bin/env python3
"""
Knowledge base retrieve script using Amazon Bedrock RAG
"""

import boto3
import logging
import sys
import os
import json
from urllib import parse
from typing import Optional
logging.basicConfig(
    level=logging.INFO,
    format='%(filename)s:%(lineno)d | %(message)s',
    handlers=[
        logging.StreamHandler(sys.stderr)
    ]
)
logger = logging.getLogger("retrieve")


NUMBER_OF_RESULTS = 5
DOC_PREFIX = "docs/"

def create_bedrock_client(region):
    return boto3.client(
        "bedrock-agent-runtime",
        region_name=region
    )


def get_knowledge_base_id_by_name(name: str, region: str) -> Optional[str]:
    """Look up knowledge base ID by name using boto3."""
    agent_client = boto3.client("bedrock-agent", region_name=region)
    next_token = None

    while True:
        kwargs = {}
        if next_token:
            kwargs["nextToken"] = next_token

        response = agent_client.list_knowledge_bases(**kwargs)

        for kb in response.get("knowledgeBaseSummaries", []):
            if kb["name"] == name:
                return kb["knowledgeBaseId"]

        next_token = response.get("nextToken")
        if not next_token:
            break

    logger.error(f"Could not find knowledge base with name: {name}")
    return None


def get_sharing_url_from_cloudfront(project_name: str) -> str:
    """Find CloudFront distribution by Comment and return https URL from DomainName."""
    cf = boto3.client("cloudfront")
    target_comment = f"{project_name} CloudFront"

    paginator = cf.get_paginator("list_distributions")
    for page in paginator.paginate():
        dist_list = page.get("DistributionList", {})
        for item in dist_list.get("Items", []):
            if item.get("Comment") == target_comment:
                domain = item.get("DomainName")
                if domain:
                    return f"https://{domain}"
    raise ValueError(f'CloudFront distribution with Comment "{target_comment}" not found')

project_name = "openclaw"
knowledge_base_name = project_name
knowledge_base_id = ""
region = "us-west-2"
sharing_url = ""

def retrieve(query):
    global sharing_url, knowledge_base_id

    if not sharing_url:
        sharing_url = get_sharing_url_from_cloudfront(project_name)

    if not knowledge_base_id:
        knowledge_base_id = get_knowledge_base_id_by_name(knowledge_base_name, region)
        if not knowledge_base_id:
            raise ValueError(f'Knowledge base "{knowledge_base_name}" not found')

    client = create_bedrock_client(region)

    retrieval_params = {
        "retrievalQuery": {"text": query},
        "knowledgeBaseId": knowledge_base_id,
        "retrievalConfiguration": {
            "vectorSearchConfiguration": {"numberOfResults": NUMBER_OF_RESULTS},
        },
    }

    response = client.retrieve(**retrieval_params)

    retrieval_results = response.get("retrievalResults", [])

    json_docs = []
    for result in retrieval_results:
        text = url = name = None

        if "content" in result:
            content = result["content"]
            if "text" in content:
                text = content["text"]

        if "location" in result:
            location = result["location"]
            if "s3Location" in location:
                uri = location["s3Location"].get("uri", "")
                name = uri.split("/")[-1]
                encoded_name = parse.quote(name)
                url = f"{sharing_url}/{DOC_PREFIX}{encoded_name}"
            elif "webLocation" in location:
                url = location["webLocation"].get("url", "")
                name = "WEB"

        json_docs.append({
            "contents": text,
            "reference": {
                "url": url,
                "title": name,
                "from": "RAG"
            }
        })

    logger.info(f"Retrieved {len(json_docs)} results")
    return json.dumps(json_docs, ensure_ascii=False)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python retrieve_search.py <keyword>")
        sys.exit(1)

    keyword = sys.argv[1]
    result = retrieve(keyword)
    print(result)

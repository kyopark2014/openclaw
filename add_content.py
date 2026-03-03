#!/usr/bin/env python3
"""
Upload content to S3 and sync Knowledge Base data source
"""

import boto3
import os
import logging
from botocore.exceptions import ClientError

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

region = "us-west-2"
project = "openclaw"

sts = boto3.client('sts', region_name=region)
response = sts.get_caller_identity()
account_id = response['Account']
    
def check_file_exists_in_s3(s3_client, bucket_name, key):
    """Check if file already exists in S3"""
    try:
        s3_client.head_object(Bucket=bucket_name, Key=key)
        return True
    except ClientError as e:
        if e.response['Error']['Code'] == '404':
            return False
        raise

def get_contents_type(file_name):
    if file_name.lower().endswith((".jpg", ".jpeg")):
        content_type = "image/jpeg"
    elif file_name.lower().endswith((".pdf")):
        content_type = "application/pdf"
    elif file_name.lower().endswith((".txt")):
        content_type = "text/plain"
    elif file_name.lower().endswith((".csv")):
        content_type = "text/csv"
    elif file_name.lower().endswith((".ppt", ".pptx")):
        content_type = "application/vnd.ms-powerpoint"
    elif file_name.lower().endswith((".doc", ".docx")):
        content_type = "application/msword"
    elif file_name.lower().endswith((".xls")):
        content_type = "application/vnd.ms-excel"
    elif file_name.lower().endswith((".py")):
        content_type = "text/x-python"
    elif file_name.lower().endswith((".js")):
        content_type = "application/javascript"
    elif file_name.lower().endswith((".md")):
        content_type = "text/markdown"
    elif file_name.lower().endswith((".png")):
        content_type = "image/png"
    else:
        content_type = "no info"    
    return content_type

def upload_file_to_s3(s3_client, local_file, bucket_name, s3_key):
    """Upload file to S3"""
    try:
        # Read file content
        with open(local_file, 'rb') as f:
            file_bytes = f.read()
        
        content_type = get_contents_type(s3_key)
        logger.info(f"Uploading {local_file} to s3://{bucket_name}/{s3_key}")
        logger.info(f"Content type: {content_type}")

        # Prepare metadata
        user_meta = {  # user-defined metadata
            "content_type": content_type
        }
        
        # Prepare put_object parameters
        put_params = {
            'Bucket': bucket_name,
            'Key': s3_key,
            'Body': file_bytes,
            'Metadata': user_meta
        }
        
        # Set ContentType if it's not "no info"
        if content_type != "no info":
            put_params['ContentType'] = content_type
        
        # Set ContentDisposition to "inline" so browser displays the file instead of downloading
        # For PDF files, this allows them to be viewed directly in the browser
        if content_type == "application/pdf":
            put_params['ContentDisposition'] = 'inline'
        
        # Upload to S3
        response = s3_client.put_object(**put_params)
        logger.info(f"✓ Successfully uploaded to S3. ETag: {response.get('ETag', 'N/A')}")

        return True
    
    except FileNotFoundError:
        logger.error(f"File not found: {local_file}")
        return False
    except Exception as e:
        logger.error(f"Error uploading to S3: {str(e)}")
        return False


def sync_knowledge_base(bedrock_client, knowledge_base_id):
    """Sync Knowledge Base data source"""
    try:
        # Get data sources for the knowledge base
        response = bedrock_client.list_data_sources(knowledgeBaseId=knowledge_base_id)
        
        if not response['dataSourceSummaries']:
            logger.error("No data sources found for knowledge base")
            return False
            
        data_source_id = response['dataSourceSummaries'][0]['dataSourceId']
        
        # Start ingestion job
        ingestion_response = bedrock_client.start_ingestion_job(
            knowledgeBaseId=knowledge_base_id,
            dataSourceId=data_source_id
        )
        
        job_id = ingestion_response['ingestionJob']['ingestionJobId']
        logger.info(f"✓ Started ingestion job: {job_id}")
        return True
        
    except Exception as e:
        logger.error(f"Failed to sync knowledge base: {e}")
        return False

def get_knowledge_base_id_by_name(name: str, region: str) -> str:
    """Get knowledge base ID by name"""
    bedrock_client = boto3.client('bedrock-agent', region_name=region)
    response = bedrock_client.list_knowledge_bases()
    for kb in response['knowledgeBaseSummaries']:
        if kb['name'] == name:
            return kb['knowledgeBaseId']
    return None

def main():
    # Load configuration
    s3_bucket = f"storage-for-{project}-{account_id}-{region}"
    knowledge_base_id = get_knowledge_base_id_by_name(project, region)
    if not knowledge_base_id:
        logger.error(f"Knowledge base with name {project} not found")
        return False
    
    # Initialize AWS clients
    s3_client = boto3.client('s3', region_name=region)
    bedrock_client = boto3.client('bedrock-agent', region_name=region)
    
    contents_dir = "contents"
    if not os.path.isdir(contents_dir):
        logger.error(f"Contents directory not found: {contents_dir}")
        return False
    
    # Upload all files in contents folder (including subdirectories)
    all_success = True
    for root, dirs, files in os.walk(contents_dir):
        for file in files:
            local_file = os.path.join(root, file)
            rel_path = os.path.relpath(local_file, contents_dir)
            s3_key = f"docs/{rel_path}".replace(os.sep, "/")
            
            if check_file_exists_in_s3(s3_client, s3_bucket, s3_key):
                logger.info(f"File already exists in S3, skipping upload: {s3_key}")
            else:
                if not upload_file_to_s3(s3_client, local_file, s3_bucket, s3_key):
                    all_success = False
    
    if not all_success:
        return False
    
    # Sync Knowledge Base
    if sync_knowledge_base(bedrock_client, knowledge_base_id):
        logger.info("✓ Knowledge Base sync initiated successfully")
        return True
    else:
        return False

if __name__ == "__main__":
    success = main()
    exit(0 if success else 1)

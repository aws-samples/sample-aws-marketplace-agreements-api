"""
MP-Buyer Sync Lambda

Syncs AWS Marketplace Agreement data into DynamoDB on a schedule (EventBridge).
Calls Agreement API + Discovery API, enriches with product names, and stores
all data in DynamoDB for fast report generation.

Triggered by: EventBridge rule (every 6 hours) or manual invocation.
"""

import json
import os
import logging
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Config
AWS_REGION_NAME = os.environ.get("AWS_REGION_NAME", "us-east-1")
TABLE_NAME = os.environ.get("TABLE_NAME", "mp-buyer-agreements")

# AWS clients
mp_client = boto3.client("marketplace-agreement", region_name=AWS_REGION_NAME)
discovery_client = boto3.client("marketplace-discovery", region_name=AWS_REGION_NAME)
dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION_NAME)
table = dynamodb.Table(TABLE_NAME)


def _get_product_name(product_id):
    """Get product name from Discovery API."""
    if not product_id:
        return None
    try:
        response = discovery_client.get_product(productId=product_id)
        return response.get("productName")
    except Exception as e:
        logger.info(f"Cannot get product name for {product_id}: {e}")
        return None


def _convert_for_dynamo(obj):
    """Convert Python objects to DynamoDB-compatible types."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    elif isinstance(obj, float):
        return Decimal(str(obj))
    elif isinstance(obj, dict):
        return {k: _convert_for_dynamo(v) for k, v in obj.items() if v is not None}
    elif isinstance(obj, list):
        return [_convert_for_dynamo(item) for item in obj]
    elif obj is None:
        return None
    return obj


def _get_all_agreements():
    """Fetch all agreements (both Acceptor and Proposer) with pagination."""
    all_agreements = []

    for party_type in ["Acceptor", "Proposer"]:
        next_token = None
        while True:
            params = {
                "catalog": "AWSMarketplace",
                "maxResults": 50,
                "filters": [
                    {"name": "AgreementType", "values": ["PurchaseAgreement"]},
                    {"name": "PartyType", "values": [party_type]},
                ],
            }
            if next_token:
                params["nextToken"] = next_token

            try:
                response = mp_client.search_agreements(**params)
                agreements = response.get("agreementViewSummaries", [])
                for agmt in agreements:
                    agmt["_partyType"] = party_type
                all_agreements.extend(agreements)
                next_token = response.get("nextToken")
                if not next_token:
                    break
            except ClientError as e:
                logger.error(f"Error searching agreements ({party_type}): {e}")
                break

    return all_agreements


def _describe_agreement(agreement_id):
    """Get full agreement details."""
    try:
        response = mp_client.describe_agreement(agreementId=agreement_id)
        response.pop("ResponseMetadata", None)
        return response
    except Exception as e:
        logger.warning(f"Cannot describe {agreement_id}: {e}")
        return None


def _get_agreement_terms(agreement_id):
    """Get agreement terms."""
    try:
        all_terms = []
        next_token = None
        while True:
            params = {"agreementId": agreement_id, "maxResults": 50}
            if next_token:
                params["nextToken"] = next_token
            response = mp_client.get_agreement_terms(**params)
            all_terms.extend(response.get("acceptedTerms", []))
            next_token = response.get("nextToken")
            if not next_token:
                break
        return all_terms
    except Exception as e:
        logger.warning(f"Cannot get terms for {agreement_id}: {e}")
        return []


def _sync_agreement(agmt):
    """Sync a single agreement to DynamoDB."""
    agreement_id = agmt.get("agreementId")
    if not agreement_id:
        return False

    now = datetime.now(timezone.utc).isoformat()

    # Get product info from resources
    resources = agmt.get("proposalSummary", {}).get("resources", [])
    product_name = None
    product_type = None
    product_id = None

    if resources:
        product_id = resources[0].get("id")
        product_type = resources[0].get("type")
        product_name = _get_product_name(product_id)

    # Get full details
    details = _describe_agreement(agreement_id)
    estimated_value = None
    currency_code = None
    if details and details.get("estimatedCharges"):
        try:
            estimated_value = Decimal(str(details["estimatedCharges"].get("agreementValue", "0")))
            currency_code = details["estimatedCharges"].get("currencyCode", "USD")
        except Exception:
            pass

    # Get terms
    terms = _get_agreement_terms(agreement_id)

    # Build DynamoDB item
    item = {
        "PK": f"AGMT#{agreement_id}",
        "SK": "METADATA",
        "agreementId": agreement_id,
        "status": agmt.get("status"),
        "agreementType": agmt.get("agreementType"),
        "partyType": agmt.get("_partyType", "Acceptor"),
        "productName": product_name or "Unknown",
        "productType": product_type or "Unknown",
        "productId": product_id,
        "proposer": agmt.get("proposer", {}).get("accountId"),
        "acceptor": agmt.get("acceptor", {}).get("accountId"),
        "offerId": agmt.get("proposalSummary", {}).get("offerId"),
        "startTime": agmt.get("startTime"),
        "endTime": agmt.get("endTime"),
        "acceptanceTime": agmt.get("acceptanceTime"),
        "estimatedValue": estimated_value,
        "currencyCode": currency_code,
        "termCount": len(terms),
        "lastSyncedAt": now,
    }

    # Convert all values for DynamoDB
    item = _convert_for_dynamo(item)

    # Remove None values (DynamoDB doesn't accept None)
    item = {k: v for k, v in item.items() if v is not None}

    # Write metadata
    table.put_item(Item=item)

    # Write terms as separate item
    if terms:
        terms_item = {
            "PK": f"AGMT#{agreement_id}",
            "SK": "TERMS",
            "agreementId": agreement_id,
            "terms": json.dumps(_convert_for_dynamo(terms), default=str),
            "termCount": len(terms),
            "lastSyncedAt": now,
        }
        table.put_item(Item=terms_item)

    # Write history record (for tracking changes over time)
    history_item = {
        "PK": f"AGMT#{agreement_id}",
        "SK": f"HISTORY#{now[:10]}",
        "agreementId": agreement_id,
        "status": agmt.get("status"),
        "estimatedValue": estimated_value,
        "syncedAt": now,
    }
    history_item = {k: v for k, v in history_item.items() if v is not None}
    table.put_item(Item=history_item)

    return True


def lambda_handler(event, context):
    """Main handler - syncs all agreements to DynamoDB."""
    logger.info(f"Sync started at {datetime.now(timezone.utc).isoformat()}")
    logger.info(f"Table: {TABLE_NAME}")

    start_time = datetime.now(timezone.utc)

    # Fetch all agreements
    agreements = _get_all_agreements()
    logger.info(f"Found {len(agreements)} agreements to sync")

    # Sync each agreement
    synced = 0
    errors = 0
    for agmt in agreements:
        try:
            if _sync_agreement(agmt):
                synced += 1
        except Exception as e:
            errors += 1
            logger.error(f"Error syncing {agmt.get('agreementId')}: {e}")

    # Write sync metadata
    elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
    sync_record = {
        "PK": "SYNC#LATEST",
        "SK": "METADATA",
        "lastSyncTime": datetime.now(timezone.utc).isoformat(),
        "totalAgreements": len(agreements),
        "synced": synced,
        "errors": errors,
        "durationSeconds": Decimal(str(round(elapsed, 1))),
    }
    table.put_item(Item=sync_record)

    result = {
        "status": "complete",
        "totalAgreements": len(agreements),
        "synced": synced,
        "errors": errors,
        "durationSeconds": round(elapsed, 1),
    }

    logger.info(f"Sync complete: {json.dumps(result)}")
    return result

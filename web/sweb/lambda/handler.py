"""
MP-Buyer Serverless Lambda Handler

Connects to the AWS Marketplace Agreement and Discovery APIs to search,
describe, and get terms for agreements, and discover products.
"""

import json
import os
import logging
from datetime import datetime

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Environment variables
AWS_REGION_NAME = os.environ.get("AWS_REGION_NAME", "us-east-1")

# AWS clients
mp_client = boto3.client("marketplace-agreement", region_name=AWS_REGION_NAME)
discovery_client = boto3.client("marketplace-discovery", region_name=AWS_REGION_NAME)


def _convert_datetimes(obj):
    """Recursively convert datetime objects to ISO format strings."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    elif isinstance(obj, dict):
        return {k: _convert_datetimes(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_convert_datetimes(item) for item in obj]
    return obj


def _cors_response(status_code, body):
    """Return response with CORS headers."""
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
        },
        "body": json.dumps(body),
    }


def _get_product_name(entity_id, entity_type=None):
    """
    Get product name using the AWS Marketplace Discovery API (GetProduct).
    
    The Discovery API is buyer-accessible and returns product details
    for any product in the marketplace catalog, unlike the Catalog API
    which only works for products you own.
    
    Args:
        entity_id: The product/resource ID from the agreement
        entity_type: The entity type (e.g., AmiProduct, SaaSProduct)
    
    Returns:
        Product name string, or None if not found
    """
    if not entity_id:
        return None

    try:
        response = discovery_client.get_product(productId=entity_id)
        product_name = response.get("productName")
        if product_name:
            return product_name

        return None

    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")
        if error_code in ("ResourceNotFoundException", "AccessDeniedException", "ValidationException"):
            logger.info(f"Cannot get product name for {entity_id}: {error_code}")
        else:
            logger.warning(f"Error getting product name for {entity_id}: {e}")
        return None
    except Exception as e:
        logger.warning(f"Unexpected error getting product name for {entity_id}: {e}")
        return None


def search_agreements(event):
    """Search for agreements with filters."""
    try:
        body = json.loads(event.get("body", "{}")) if event.get("body") else {}

        params = {
            "catalog": body.get("catalog", "AWSMarketplace"),
            "maxResults": body.get("max_results", 50),
        }

        filters = body.get("filters")
        if filters:
            params["filters"] = filters

        next_token = body.get("next_token")
        if next_token:
            params["nextToken"] = next_token

        sort_by = body.get("sort_by")
        sort_order = body.get("sort_order")
        if sort_by or sort_order:
            sort_config = {}
            if sort_by:
                sort_config["sortBy"] = sort_by
            if sort_order:
                sort_config["sortOrder"] = sort_order
            params["sort"] = sort_config

        response = mp_client.search_agreements(**params)
        result = _convert_datetimes({
            "agreementViewSummaries": response.get("agreementViewSummaries", []),
            "nextToken": response.get("nextToken"),
            "count": len(response.get("agreementViewSummaries", [])),
        })

        # Enrich with product names (best-effort, don't fail if lookup fails)
        for agmt in result.get("agreementViewSummaries", []):
            resources = agmt.get("proposalSummary", {}).get("resources", [])
            for resource in resources:
                product_name = _get_product_name(resource.get("id"), resource.get("type"))
                if product_name:
                    resource["name"] = product_name

        return _cors_response(200, result)

    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "Unknown")
        error_message = e.response.get("Error", {}).get("Message", str(e))
        logger.error(f"ClientError: {error_code} - {error_message}")
        return _cors_response(400, {"error": f"{error_code}: {error_message}"})
    except Exception as e:
        logger.error(f"Error: {str(e)}")
        return _cors_response(500, {"error": str(e)})


def describe_agreement(event):
    """Get details for a specific agreement."""
    try:
        path_params = event.get("pathParameters", {}) or {}
        agreement_id = path_params.get("agreementId", "")

        if not agreement_id:
            return _cors_response(400, {"error": "Missing agreementId"})

        response = mp_client.describe_agreement(agreementId=agreement_id)
        response.pop("ResponseMetadata", None)
        result = _convert_datetimes(response)

        # Enrich with product names from Catalog API
        if result.get("proposalSummary", {}).get("resources"):
            for resource in result["proposalSummary"]["resources"]:
                product_name = _get_product_name(resource.get("id"), resource.get("type"))
                if product_name:
                    resource["name"] = product_name

        return _cors_response(200, result)

    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "Unknown")
        error_message = e.response.get("Error", {}).get("Message", str(e))
        logger.error(f"ClientError: {error_code} - {error_message}")
        return _cors_response(400, {"error": f"{error_code}: {error_message}"})
    except Exception as e:
        logger.error(f"Error: {str(e)}")
        return _cors_response(500, {"error": str(e)})


def get_agreement_terms(event):
    """Get terms for a specific agreement."""
    try:
        path_params = event.get("pathParameters", {}) or {}
        agreement_id = path_params.get("agreementId", "")

        if not agreement_id:
            return _cors_response(400, {"error": "Missing agreementId"})

        query_params = event.get("queryStringParameters", {}) or {}
        max_results = int(query_params.get("max_results", "50"))
        next_token = query_params.get("next_token")

        params = {"agreementId": agreement_id, "maxResults": max_results}
        if next_token:
            params["nextToken"] = next_token

        response = mp_client.get_agreement_terms(**params)
        result = _convert_datetimes({
            "acceptedTerms": response.get("acceptedTerms", []),
            "nextToken": response.get("nextToken"),
            "count": len(response.get("acceptedTerms", [])),
        })
        return _cors_response(200, result)

    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "Unknown")
        error_message = e.response.get("Error", {}).get("Message", str(e))
        logger.error(f"ClientError: {error_code} - {error_message}")
        return _cors_response(400, {"error": f"{error_code}: {error_message}"})
    except Exception as e:
        logger.error(f"Error: {str(e)}")
        return _cors_response(500, {"error": str(e)})


def health_check(event):
    """Health check endpoint."""
    try:
        sts = boto3.client("sts")
        identity = sts.get_caller_identity()
        return _cors_response(200, {
            "status": "healthy",
            "account": identity["Account"],
            "region": AWS_REGION_NAME,
        })
    except Exception as e:
        return _cors_response(500, {"status": "unhealthy", "error": str(e)})


def search_products(event):
    """Search for products in AWS Marketplace using Discovery API."""
    try:
        body = json.loads(event.get("body", "{}")) if event.get("body") else {}
        search_text = body.get("search_text", "")
        max_results = body.get("max_results", 20)
        filters = body.get("filters")

        params = {"maxResults": max_results}
        if search_text:
            params["searchText"] = search_text
        if filters:
            params["filters"] = filters

        response = discovery_client.search_listings(**params)

        listings = []
        for listing in response.get("listingSummaries", []):
            product_info = {}
            # Extract product details from associatedEntities
            for entity in listing.get("associatedEntities", []):
                product = entity.get("product", {})
                if product:
                    product_info = {
                        "productId": product.get("productId"),
                        "productName": product.get("productName"),
                        "manufacturer": product.get("manufacturer", {}).get("displayName"),
                    }

            listings.append({
                "listingId": listing.get("listingId"),
                "listingName": listing.get("listingName"),
                "shortDescription": listing.get("shortDescription"),
                "logoUrl": listing.get("logoThumbnailUrl"),
                "publisher": listing.get("publisher", {}).get("displayName"),
                "categories": [c.get("displayName") for c in listing.get("categories", [])],
                "pricingModels": [p.get("displayName") for p in listing.get("pricingModels", [])],
                "fulfillmentOptions": [f.get("displayName") for f in listing.get("fulfillmentOptionSummaries", [])],
                "badges": [b.get("displayName") for b in listing.get("badges", [])],
                "product": product_info,
            })

        return _cors_response(200, {
            "listings": listings,
            "totalResults": response.get("totalResults", 0),
            "nextToken": response.get("nextToken"),
            "count": len(listings),
        })

    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "Unknown")
        error_message = e.response.get("Error", {}).get("Message", str(e))
        logger.error(f"ClientError in search_products: {error_code} - {error_message}")
        return _cors_response(400, {"error": f"{error_code}: {error_message}"})
    except Exception as e:
        logger.error(f"Error in search_products: {str(e)}")
        return _cors_response(500, {"error": str(e)})


def get_purchase_options(event):
    """Get available purchase options (offers) for a product."""
    try:
        path_params = event.get("pathParameters", {}) or {}
        product_id = path_params.get("productId", "")

        if not product_id:
            return _cors_response(400, {"error": "Missing productId"})

        response = discovery_client.list_purchase_options(
            filters=[
                {"filterType": "PRODUCT_ID", "filterValues": [product_id]}
            ],
            maxResults=20
        )

        # Remove boto3 metadata
        response.pop("ResponseMetadata", None)

        # Log raw response keys for debugging
        logger.info(f"ListPurchaseOptions response keys: {list(response.keys())}")
        logger.info(f"ListPurchaseOptions raw response: {json.dumps(str(response)[:2000])}")

        # Try multiple possible response keys
        raw_options = (
            response.get("purchaseOptionSummaries")
            or response.get("purchaseOptions")
            or response.get("PurchaseOptionSummaries")
            or response.get("PurchaseOptions")
            or []
        )

        options = []
        for option in raw_options:
            # Log first option structure for debugging
            if not options:
                logger.info(f"First purchase option: {json.dumps(str(option)[:1000])}")

            # Extract offer info from associatedEntities
            associated = option.get("associatedEntities", [])
            offer_info = {}
            for entity in associated:
                if "offer" in entity:
                    offer_info = entity["offer"]
                elif "Offer" in entity:
                    offer_info = entity["Offer"]

            seller_record = option.get("sellerOfRecord", {})

            options.append({
                "offerId": option.get("purchaseOptionId") or offer_info.get("offerId"),
                "offerName": offer_info.get("offerName") or offer_info.get("name") or option.get("purchaseOptionId"),
                "purchaseOptionType": option.get("purchaseOptionType"),
                "pricingSummary": offer_info.get("pricingSummary") or offer_info.get("pricing"),
                "availabilityStatus": "AVAILABLE" if option.get("availableFromTime") else None,
                "availableFrom": option.get("availableFromTime"),
                "seller": seller_record.get("displayName") or seller_record.get("name") if isinstance(seller_record, dict) else str(seller_record),
                "badges": [b.get("displayName") or b.get("badgeType") for b in option.get("badges", [])],
                "agreementProposalIdentifier": option.get("purchaseOptionId"),
                "catalog": option.get("catalog"),
            })

        # If no options found, return the raw response for debugging
        if not options and response:
            return _cors_response(200, {
                "purchaseOptions": [],
                "count": 0,
                "debug_keys": list(response.keys()),
                "debug_sample": str(response)[:1000],
            })

        return _cors_response(200, _convert_datetimes({
            "purchaseOptions": options,
            "count": len(options),
        }))

    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "Unknown")
        error_message = e.response.get("Error", {}).get("Message", str(e))
        logger.error(f"ClientError in get_purchase_options: {error_code} - {error_message}")
        return _cors_response(400, {"error": f"{error_code}: {error_message}"})
    except Exception as e:
        logger.error(f"Error in get_purchase_options: {str(e)}")
        return _cors_response(500, {"error": str(e)})


def create_subscription(event):
    """Create an agreement request (quote) for a product subscription.
    
    Flow: GetOffer → GetOfferTerms → CreateAgreementRequest
    The agreementProposalIdentifier comes from GetOffer, not ListPurchaseOptions.
    """
    try:
        body = json.loads(event.get("body", "{}")) if event.get("body") else {}

        offer_id = body.get("offerId") or body.get("agreementProposalIdentifier")
        intent = body.get("intent", "NEW")

        if not offer_id:
            return _cors_response(400, {"error": "Missing offerId"})

        # Step 1: Get the offer to obtain agreementProposalId
        logger.info(f"Getting offer details for: {offer_id}")
        offer_response = discovery_client.get_offer(offerId=offer_id)
        
        agreement_proposal_id = offer_response.get("agreementProposalId")
        if not agreement_proposal_id:
            return _cors_response(400, {
                "error": "No agreementProposalId found in offer. This offer may not support programmatic subscription.",
                "offerKeys": list(offer_response.keys()),
            })

        # Step 2: Get offer terms to build requestedTerms
        logger.info(f"Getting offer terms for: {offer_id}")
        terms_response = discovery_client.get_offer_terms(offerId=offer_id)
        
        offer_terms = terms_response.get("offerTerms", [])
        if not offer_terms:
            return _cors_response(400, {"error": "No terms found for this offer"})

        # Step 3: Build requestedTerms from offer terms
        # Include all terms; add default configuration for configurable terms
        requested_terms = []
        for term_wrapper in offer_terms:
            for term_type, term_data in term_wrapper.items():
                if not isinstance(term_data, dict):
                    continue
                term_id = term_data.get("id")
                if not term_id:
                    continue

                term_entry = {"id": term_id}

                # Add default configuration for RenewalTerm
                if "renewalTerm" in term_type.lower() or term_data.get("type") == "RenewalTerm":
                    term_entry["configuration"] = {
                        "renewalTermConfiguration": {"enableAutoRenew": False}
                    }

                requested_terms.append(term_entry)

        if not requested_terms:
            return _cors_response(400, {
                "error": "Could not extract term IDs from offer",
                "offerTermsSample": str(offer_terms[:2])[:500],
            })

        # Step 4: Create the agreement request
        logger.info(f"Creating agreement request with proposal: {agreement_proposal_id}, terms: {len(requested_terms)}")
        
        import uuid
        params = {
            "intent": intent,
            "agreementProposalIdentifier": agreement_proposal_id,
            "requestedTerms": requested_terms,
            "clientToken": str(uuid.uuid4()),
        }

        source_agreement = body.get("sourceAgreementIdentifier")
        if source_agreement:
            params["sourceAgreementIdentifier"] = source_agreement

        response = mp_client.create_agreement_request(**params)

        result = _convert_datetimes({
            "agreementRequestId": response.get("agreementRequestId"),
            "chargeSummary": response.get("chargeSummary"),
        })

        return _cors_response(200, result)

    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "Unknown")
        error_message = e.response.get("Error", {}).get("Message", str(e))
        logger.error(f"ClientError in create_subscription: {error_code} - {error_message}")
        return _cors_response(400, {"error": f"{error_code}: {error_message}"})
    except Exception as e:
        logger.error(f"Error in create_subscription: {str(e)}")
        return _cors_response(500, {"error": str(e)})


def accept_subscription(event):
    """Accept an agreement request to finalize the subscription."""
    try:
        body = json.loads(event.get("body", "{}")) if event.get("body") else {}

        agreement_request_id = body.get("agreementRequestId")
        if not agreement_request_id:
            return _cors_response(400, {"error": "Missing agreementRequestId"})

        response = mp_client.accept_agreement_request(
            agreementRequestId=agreement_request_id
        )

        result = _convert_datetimes({
            "agreementId": response.get("agreementId"),
            "status": "ACCEPTED",
        })

        return _cors_response(200, result)

    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "Unknown")
        error_message = e.response.get("Error", {}).get("Message", str(e))
        logger.error(f"ClientError in accept_subscription: {error_code} - {error_message}")
        return _cors_response(400, {"error": f"{error_code}: {error_message}"})
    except Exception as e:
        logger.error(f"Error in accept_subscription: {str(e)}")
        return _cors_response(500, {"error": str(e)})


def lambda_handler(event, context):
    """Main Lambda handler - routes requests based on path and method."""
    logger.info(f"Event: {json.dumps(event)}")

    http_method = event.get("httpMethod", "GET")
    path = event.get("path", "/")
    resource = event.get("resource", path)

    # Handle CORS preflight
    if http_method == "OPTIONS":
        return _cors_response(200, {"message": "OK"})

    # Route requests
    if resource == "/api/health" or path == "/api/health":
        return health_check(event)

    elif resource == "/api/products/search" or path == "/api/products/search":
        return search_products(event)

    elif "/api/products/" in (resource or path) and "/options" in (resource or path):
        return get_purchase_options(event)

    elif resource == "/api/subscribe" or path == "/api/subscribe":
        return create_subscription(event)

    elif resource == "/api/subscribe/accept" or path == "/api/subscribe/accept":
        return accept_subscription(event)

    elif resource == "/api/agreements/search" or path == "/api/agreements/search":
        return search_agreements(event)

    elif "/api/agreements/" in (resource or path):
        if "/terms" in (resource or path):
            return get_agreement_terms(event)
        else:
            return describe_agreement(event)

    else:
        return _cors_response(404, {"error": f"Not found: {path}"})

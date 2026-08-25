"""
MP-Buyer Agreements Reporting Agent

Generates reports from AWS Marketplace Agreement and Discovery APIs.
Uses Strands Agents SDK for AI-powered report generation and analysis.

Reports:
  - spend_summary: Total spend by vendor, category, product type
  - expiring_soon: Agreements expiring in next N days
  - portfolio: Full inventory of active subscriptions
  - lifecycle: Subscription trends, churn, renewals
  - compliance: Terms audit, EULA status, support levels

Triggered via API Gateway or EventBridge schedule.
"""

import json
import os
import logging
from datetime import datetime, timedelta, timezone
from collections import defaultdict

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Config
AWS_REGION_NAME = os.environ.get("AWS_REGION_NAME", "us-east-1")
REPORT_BUCKET = os.environ.get("REPORT_BUCKET", "")
TABLE_NAME = os.environ.get("TABLE_NAME", "mp-buyer-agreements")
USE_STRANDS = os.environ.get("USE_STRANDS", "false").lower() == "true"

# AWS clients
mp_client = boto3.client("marketplace-agreement", region_name=AWS_REGION_NAME)
discovery_client = boto3.client("marketplace-discovery", region_name=AWS_REGION_NAME)
s3_client = boto3.client("s3", region_name=AWS_REGION_NAME)
dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION_NAME)
table = dynamodb.Table(TABLE_NAME)

# Optional: Strands agent for AI-powered analysis
strands_agent = None
if USE_STRANDS:
    try:
        from strands import Agent
        from strands.models.bedrock import BedrockModel

        model = BedrockModel(
            model_id="anthropic.claude-sonnet-4-20250514",
            region_name=AWS_REGION_NAME,
        )
        strands_agent = Agent(
            model=model,
            system_prompt="""You are an AWS Marketplace procurement analyst. 
            Analyze agreement data and provide concise, actionable insights.
            Focus on cost optimization, renewal risks, and vendor consolidation opportunities.
            Format reports with clear sections, bullet points, and key metrics.""",
        )
        logger.info("Strands agent initialized")
    except Exception as e:
        logger.warning(f"Strands not available, using basic reports: {e}")


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
        "body": json.dumps(body, default=str),
    }


# =============================================================================
# Data Collection
# =============================================================================

def _get_all_agreements(party_type="Acceptor", status=None):
    """Fetch all agreements from DynamoDB (fast) with fallback to API."""
    try:
        # Try DynamoDB first
        scan_params = {
            "FilterExpression": boto3.dynamodb.conditions.Attr("SK").eq("METADATA") & boto3.dynamodb.conditions.Attr("PK").begins_with("AGMT#"),
        }

        if party_type:
            scan_params["FilterExpression"] = scan_params["FilterExpression"] & boto3.dynamodb.conditions.Attr("partyType").eq(party_type)
        if status:
            scan_params["FilterExpression"] = scan_params["FilterExpression"] & boto3.dynamodb.conditions.Attr("status").eq(status)

        # Need to import Key/Attr
        from boto3.dynamodb.conditions import Attr, Key

        filter_expr = Attr("SK").eq("METADATA") & Attr("PK").begins_with("AGMT#")
        if party_type:
            filter_expr = filter_expr & Attr("partyType").eq(party_type)
        if status:
            filter_expr = filter_expr & Attr("status").eq(status)

        items = []
        response = table.scan(FilterExpression=filter_expr)
        items.extend(response.get("Items", []))

        while "LastEvaluatedKey" in response:
            response = table.scan(
                FilterExpression=filter_expr,
                ExclusiveStartKey=response["LastEvaluatedKey"]
            )
            items.extend(response.get("Items", []))

        if items:
            logger.info(f"Loaded {len(items)} agreements from DynamoDB")
            # Convert DynamoDB format to match API format for report generators
            agreements = []
            for item in items:
                agreements.append({
                    "agreementId": item.get("agreementId"),
                    "status": item.get("status"),
                    "agreementType": item.get("agreementType"),
                    "proposer": {"accountId": item.get("proposer")},
                    "acceptor": {"accountId": item.get("acceptor")},
                    "startTime": item.get("startTime"),
                    "endTime": item.get("endTime"),
                    "proposalSummary": {
                        "offerId": item.get("offerId"),
                        "resources": [{
                            "id": item.get("productId"),
                            "type": item.get("productType"),
                            "name": item.get("productName"),
                        }]
                    },
                    "_estimatedValue": float(item["estimatedValue"]) if item.get("estimatedValue") else None,
                    "_currencyCode": item.get("currencyCode"),
                    "_termCount": int(item.get("termCount", 0)),
                })
            return agreements

    except Exception as e:
        logger.warning(f"DynamoDB read failed, falling back to API: {e}")

    # Fallback to live API
    all_agreements = []
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
        if status:
            params["filters"].append({"name": "Status", "values": [status]})
        if next_token:
            params["nextToken"] = next_token

        response = mp_client.search_agreements(**params)
        agreements = response.get("agreementViewSummaries", [])
        all_agreements.extend(agreements)

        next_token = response.get("nextToken")
        if not next_token:
            break

    return all_agreements


def _get_product_name(product_id):
    """Get product name from Discovery API."""
    if not product_id:
        return None
    try:
        response = discovery_client.get_product(productId=product_id)
        return response.get("productName")
    except Exception:
        return None


def _enrich_agreements(agreements):
    """Add product names and details to agreements."""
    for agmt in agreements:
        resources = agmt.get("proposalSummary", {}).get("resources", [])
        for resource in resources:
            name = _get_product_name(resource.get("id"))
            if name:
                resource["name"] = name
    return agreements


def _get_agreement_details(agreement_id):
    """Get full agreement details including charges."""
    try:
        response = mp_client.describe_agreement(agreementId=agreement_id)
        response.pop("ResponseMetadata", None)
        return response
    except Exception as e:
        logger.warning(f"Cannot describe {agreement_id}: {e}")
        return None


# =============================================================================
# Report Generators
# =============================================================================

def generate_spend_summary(agreements):
    """Generate spend summary report by vendor, category, and type."""
    by_proposer = defaultdict(lambda: {"count": 0, "products": []})
    by_type = defaultdict(int)
    by_status = defaultdict(int)
    total_value = 0

    for agmt in agreements:
        proposer = agmt.get("proposer", {}).get("accountId", "Unknown")
        resources = agmt.get("proposalSummary", {}).get("resources", [])
        status = agmt.get("status", "Unknown")

        by_status[status] += 1
        by_proposer[proposer]["count"] += 1

        for r in resources:
            rtype = r.get("type", "Unknown")
            by_type[rtype] += 1
            by_proposer[proposer]["products"].append(r.get("name") or r.get("id"))

        # Get estimated charges - use cached value from DynamoDB if available
        if agmt.get("_estimatedValue"):
            total_value += agmt["_estimatedValue"]
        else:
            details = _get_agreement_details(agmt.get("agreementId"))
            if details and details.get("estimatedCharges"):
                charges = details["estimatedCharges"]
                try:
                    value = float(charges.get("agreementValue", 0))
                    total_value += value
                except (ValueError, TypeError):
                    pass

    return {
        "reportType": "spend_summary",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "totalAgreements": len(agreements),
        "totalEstimatedValue": total_value,
        "byStatus": dict(by_status),
        "byProductType": dict(by_type),
        "byProposer": {k: v for k, v in sorted(by_proposer.items(), key=lambda x: x[1]["count"], reverse=True)},
    }


def generate_expiring_report(agreements, days=90):
    """Generate report of agreements expiring within N days."""
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=days)
    expiring = []

    for agmt in agreements:
        if agmt.get("status") != "ACTIVE":
            continue

        end_time = agmt.get("endTime")
        if not end_time:
            continue

        if isinstance(end_time, str):
            end_time = datetime.fromisoformat(end_time.replace("Z", "+00:00"))

        if end_time <= cutoff:
            resources = agmt.get("proposalSummary", {}).get("resources", [])
            product_name = resources[0].get("name") if resources else "Unknown"
            days_remaining = (end_time - now).days

            expiring.append({
                "agreementId": agmt.get("agreementId"),
                "productName": product_name,
                "endTime": end_time.isoformat(),
                "daysRemaining": days_remaining,
                "proposer": agmt.get("proposer", {}).get("accountId"),
            })

    expiring.sort(key=lambda x: x["daysRemaining"])

    return {
        "reportType": "expiring_soon",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "lookAheadDays": days,
        "totalExpiring": len(expiring),
        "agreements": expiring,
    }


def generate_portfolio_report(agreements):
    """Generate full portfolio/inventory report."""
    active = [a for a in agreements if a.get("status") == "ACTIVE"]
    portfolio = []

    for agmt in active:
        resources = agmt.get("proposalSummary", {}).get("resources", [])
        product_name = resources[0].get("name") if resources else "Unknown"
        product_type = resources[0].get("type") if resources else "Unknown"

        portfolio.append({
            "agreementId": agmt.get("agreementId"),
            "productName": product_name,
            "productType": product_type,
            "proposer": agmt.get("proposer", {}).get("accountId"),
            "startTime": agmt.get("startTime"),
            "endTime": agmt.get("endTime"),
            "status": agmt.get("status"),
        })

    portfolio.sort(key=lambda x: x["productName"])

    # Summary stats
    types = defaultdict(int)
    vendors = defaultdict(int)
    for p in portfolio:
        types[p["productType"]] += 1
        vendors[p["proposer"]] += 1

    return {
        "reportType": "portfolio",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "totalActive": len(portfolio),
        "byProductType": dict(types),
        "uniqueVendors": len(vendors),
        "topVendors": dict(sorted(vendors.items(), key=lambda x: x[1], reverse=True)[:10]),
        "subscriptions": portfolio,
    }


def generate_lifecycle_report(agreements):
    """Generate lifecycle/trend report."""
    by_status = defaultdict(int)
    by_month_created = defaultdict(int)
    by_month_ended = defaultdict(int)

    for agmt in agreements:
        status = agmt.get("status", "Unknown")
        by_status[status] += 1

        start = agmt.get("startTime")
        if start:
            if isinstance(start, datetime):
                month_key = start.strftime("%Y-%m")
            else:
                month_key = start[:7]
            by_month_created[month_key] += 1

        end = agmt.get("endTime")
        if end and status in ("EXPIRED", "TERMINATED", "CANCELLED"):
            if isinstance(end, datetime):
                month_key = end.strftime("%Y-%m")
            else:
                month_key = end[:7]
            by_month_ended[month_key] += 1

    active = by_status.get("ACTIVE", 0)
    total = len(agreements)
    terminated = by_status.get("TERMINATED", 0) + by_status.get("CANCELLED", 0)
    churn_rate = (terminated / total * 100) if total > 0 else 0

    return {
        "reportType": "lifecycle",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "totalAgreements": total,
        "statusDistribution": dict(by_status),
        "activeCount": active,
        "churnRate": round(churn_rate, 1),
        "createdByMonth": dict(sorted(by_month_created.items())),
        "endedByMonth": dict(sorted(by_month_ended.items())),
    }


def generate_compliance_report(agreements):
    """Generate compliance/terms audit report."""
    active = [a for a in agreements if a.get("status") == "ACTIVE"]
    terms_audit = []

    for agmt in active[:20]:  # Limit to 20 to avoid throttling
        agreement_id = agmt.get("agreementId")
        try:
            terms_response = mp_client.get_agreement_terms(
                agreementId=agreement_id, maxResults=50
            )
            accepted_terms = terms_response.get("acceptedTerms", [])

            term_types = []
            has_eula = False
            has_support = False
            has_renewal = False
            has_pricing = False

            for term in accepted_terms:
                for key in term.keys():
                    term_types.append(key)
                    if "legal" in key.lower() or "eula" in key.lower():
                        has_eula = True
                    if "support" in key.lower():
                        has_support = True
                    if "renewal" in key.lower():
                        has_renewal = True
                    if "pricing" in key.lower() or "payment" in key.lower():
                        has_pricing = True

            resources = agmt.get("proposalSummary", {}).get("resources", [])
            product_name = resources[0].get("name") if resources else "Unknown"

            terms_audit.append({
                "agreementId": agreement_id,
                "productName": product_name,
                "termCount": len(accepted_terms),
                "termTypes": term_types,
                "hasEula": has_eula,
                "hasSupportTerms": has_support,
                "hasRenewalTerms": has_renewal,
                "hasPricingTerms": has_pricing,
            })

        except Exception as e:
            logger.warning(f"Cannot get terms for {agreement_id}: {e}")

    return {
        "reportType": "compliance",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "totalAudited": len(terms_audit),
        "withEula": sum(1 for t in terms_audit if t["hasEula"]),
        "withSupport": sum(1 for t in terms_audit if t["hasSupportTerms"]),
        "withRenewal": sum(1 for t in terms_audit if t["hasRenewalTerms"]),
        "agreements": terms_audit,
    }


# =============================================================================
# AI-Powered Analysis (Strands)
# =============================================================================

def generate_ai_analysis(report_data):
    """Use Strands agent to generate AI-powered insights from report data."""
    if not strands_agent:
        return None

    try:
        prompt = f"""Analyze this AWS Marketplace agreements report data and provide:
1. Executive summary (3-4 sentences)
2. Key findings (top 5 bullet points)
3. Cost optimization recommendations
4. Risk alerts (expiring agreements, vendor concentration)
5. Action items for the procurement team

Report data:
{json.dumps(report_data, indent=2, default=str)[:8000]}"""

        result = strands_agent(prompt)
        return str(result)

    except Exception as e:
        logger.error(f"Strands analysis failed: {e}")
        return None


# =============================================================================
# Report Storage
# =============================================================================

def _save_report(report, report_type):
    """Save report to both S3 and DynamoDB for retrieval."""
    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y%m%d_%H%M%S")
    report_id = f"{report_type}_{timestamp}"
    s3_key = f"reports/{report_type}/{timestamp}.json"

    # Save to S3
    s3_path = None
    if REPORT_BUCKET:
        try:
            s3_client.put_object(
                Bucket=REPORT_BUCKET,
                Key=s3_key,
                Body=json.dumps(report, indent=2, default=str),
                ContentType="application/json",
            )
            s3_path = f"s3://{REPORT_BUCKET}/{s3_key}"
            logger.info(f"Report saved to {s3_path}")
        except Exception as e:
            logger.error(f"Failed to save report to S3: {e}")

    # Save metadata to DynamoDB
    try:
        from decimal import Decimal
        table.put_item(Item={
            "PK": "REPORT#LIST",
            "SK": f"REPORT#{report_id}",
            "reportId": report_id,
            "reportType": report_type,
            "generatedAt": now.isoformat(),
            "s3Key": s3_key,
            "s3Bucket": REPORT_BUCKET,
            "status": "COMPLETED",
            "totalAgreements": report.get("totalAgreements") or report.get("totalActive") or report.get("totalExpiring") or 0,
        })
        logger.info(f"Report metadata saved to DynamoDB: {report_id}")
    except Exception as e:
        logger.error(f"Failed to save report metadata to DynamoDB: {e}")

    return {"reportId": report_id, "s3Path": s3_path, "s3Key": s3_key}


def _list_reports(max_items=50):
    """List all generated reports from DynamoDB."""
    try:
        from boto3.dynamodb.conditions import Key
        response = table.query(
            KeyConditionExpression=Key("PK").eq("REPORT#LIST"),
            ScanIndexForward=False,
            Limit=max_items,
        )
        reports = response.get("Items", [])
        return [
            {
                "reportId": r.get("reportId"),
                "reportType": r.get("reportType"),
                "generatedAt": r.get("generatedAt"),
                "status": r.get("status"),
                "totalAgreements": int(r.get("totalAgreements", 0)),
                "s3Key": r.get("s3Key"),
            }
            for r in reports
        ]
    except Exception as e:
        logger.error(f"Failed to list reports: {e}")
        return []


def _get_report_content(s3_key):
    """Retrieve a specific report from S3."""
    if not REPORT_BUCKET or not s3_key:
        return None
    try:
        response = s3_client.get_object(Bucket=REPORT_BUCKET, Key=s3_key)
        content = response["Body"].read().decode("utf-8")
        return json.loads(content)
    except Exception as e:
        logger.error(f"Failed to get report from S3: {e}")
        return None


# =============================================================================
# Lambda Handler
# =============================================================================

def lambda_handler(event, context):
    """Main handler - generates reports, lists them, or retrieves them."""
    logger.info(f"Event: {json.dumps(event, default=str)}")

    http_method = event.get("httpMethod", "")
    path = event.get("path", "")

    # Handle CORS preflight
    if http_method == "OPTIONS":
        return _cors_response(200, {"message": "OK"})

    # Parse request
    body = {}
    if event.get("body"):
        body = json.loads(event["body"])

    query_params = event.get("queryStringParameters") or {}

    # Route: GET /api/reports — list all reports
    if http_method == "GET" and not query_params.get("id"):
        reports = _list_reports()
        return _cors_response(200, {"reports": reports, "count": len(reports)})

    # Route: GET /api/reports?id=report_id — get specific report content
    if http_method == "GET" and query_params.get("id"):
        report_id = query_params["id"]
        # Find the report in DynamoDB to get s3Key
        try:
            from boto3.dynamodb.conditions import Key
            response = table.query(
                KeyConditionExpression=Key("PK").eq("REPORT#LIST") & Key("SK").eq(f"REPORT#{report_id}"),
            )
            items = response.get("Items", [])
            if not items:
                return _cors_response(404, {"error": "Report not found"})
            s3_key = items[0].get("s3Key")
            content = _get_report_content(s3_key)
            if not content:
                return _cors_response(404, {"error": "Report content not found in S3"})
            return _cors_response(200, content)
        except Exception as e:
            return _cors_response(500, {"error": str(e)})

    # Route: POST /api/reports — generate a report (async: save and return immediately)
    report_type = body.get("report_type", "portfolio")
    days = int(body.get("days", 90))
    party_type = body.get("party_type", "Acceptor")
    include_ai = body.get("include_ai", False)
    is_scheduled = body.get("scheduled", False)

    logger.info(f"Generating report: {report_type}, party: {party_type}, scheduled: {is_scheduled}")

    try:
        # Fetch all agreements
        agreements = _get_all_agreements(party_type=party_type)
        agreements = _convert_datetimes(agreements)

        # Enrich with product names
        agreements = _enrich_agreements(agreements)

        # Generate requested report
        if report_type == "spend_summary":
            report = generate_spend_summary(agreements)
        elif report_type == "expiring_soon":
            report = generate_expiring_report(agreements, days=days)
        elif report_type == "portfolio":
            report = generate_portfolio_report(agreements)
        elif report_type == "lifecycle":
            report = generate_lifecycle_report(agreements)
        elif report_type == "compliance":
            report = generate_compliance_report(agreements)
        elif report_type == "all":
            # Generate all report types individually
            for rt in ["spend_summary", "expiring_soon", "portfolio", "lifecycle"]:
                if rt == "spend_summary":
                    r = generate_spend_summary(agreements)
                elif rt == "expiring_soon":
                    r = generate_expiring_report(agreements, days=days)
                elif rt == "portfolio":
                    r = generate_portfolio_report(agreements)
                elif rt == "lifecycle":
                    r = generate_lifecycle_report(agreements)
                _save_report(r, rt)

            report = {
                "reportType": "comprehensive",
                "generatedAt": datetime.now(timezone.utc).isoformat(),
                "message": "All reports generated and saved",
            }
            _save_report(report, "comprehensive")
            return _cors_response(200, {"status": "complete", "message": "All reports generated"})
        else:
            return _cors_response(400, {"error": f"Unknown report type: {report_type}"})

        # AI analysis if requested and available
        if include_ai and strands_agent:
            ai_insights = generate_ai_analysis(report)
            if ai_insights:
                report["aiInsights"] = ai_insights

        # Save report
        save_result = _save_report(report, report_type)

        # For scheduled runs, just return success
        if is_scheduled:
            return {"status": "complete", "reportId": save_result.get("reportId")}

        # For API calls, return the report ID (don't return full report to avoid timeout)
        return _cors_response(200, {
            "status": "complete",
            "reportId": save_result.get("reportId"),
            "reportType": report_type,
            "generatedAt": report.get("generatedAt"),
            "message": "Report generated and saved. Use GET /api/reports?id=REPORT_ID to view.",
        })

    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "Unknown")
        error_message = e.response.get("Error", {}).get("Message", str(e))
        logger.error(f"ClientError: {error_code} - {error_message}")
        return _cors_response(400, {"error": f"{error_code}: {error_message}"})
    except Exception as e:
        logger.error(f"Error generating report: {str(e)}", exc_info=True)
        return _cors_response(500, {"error": str(e)})
        logger.error(f"Error generating report: {str(e)}", exc_info=True)
        return _cors_response(500, {"error": str(e)})

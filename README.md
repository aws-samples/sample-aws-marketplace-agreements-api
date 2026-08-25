# MP-Buyer

A serverless web application for managing AWS Marketplace agreements. Search and subscribe to Marketplace products, view active agreements, and generate AI-powered procurement reports — all from a single dashboard protected by Cognito authentication.

## Architecture

```
CloudFront (CDN)
  ├── S3 (Static frontend)
  └── API Gateway (REST API)
        ├── Lambda: Agreements (search, describe, terms, product discovery, subscribe)
        ├── Lambda: Reports   (AI-powered spend/lifecycle/compliance analysis via Strands)
        └── Lambda: Sync      (scheduled agreement data sync to DynamoDB)
```

Key AWS services:
- **CloudFront + S3** — serves the single-page frontend
- **API Gateway** — REST API with Cognito authorizer
- **Lambda (Python 3.11)** — three functions handling API, reports, and data sync
- **DynamoDB** — cached agreement data with GSIs for status and expiration queries
- **Cognito** — user pool with email-based sign-up and SRP authentication
- **EventBridge** — schedules data sync (every 6 hours) and report generation (3x/day)
- **Bedrock** — powers the Strands-based report agent (Claude Sonnet)
- **KMS** — customer-managed key for DynamoDB encryption at rest

## Features

- **Agreements** — Search, view details, and inspect terms of your Marketplace subscriptions
- **Product Discovery** — Search the Marketplace catalog, view purchase options, and subscribe
- **Reports** — Spend summary, expiring agreements, portfolio inventory, lifecycle trends, and compliance audits (AI-enhanced when Strands is enabled)
- **Scheduled Sync** — Keeps a local DynamoDB cache in sync with the Marketplace Agreement API

## Prerequisites

- AWS CLI configured (`aws configure`) with permissions to deploy CloudFormation, Lambda, S3, etc.
- Python 3.10+
- An AWS account with Marketplace buyer access

## Deployment

```bash
chmod +x deploy_all.sh
./deploy_all.sh [region] [admin-username] [admin-email]
```

Default region is `us-east-1`. The script will:

1. Deploy the CloudFormation stack (all infrastructure)
2. Package and deploy the three Lambda functions (with dependencies)
3. Run an initial agreement data sync
4. Generate the frontend auth config (`config.js`)
5. Attach additional IAM policies for Discovery and Subscribe APIs
6. Upload the frontend to S3
7. Invalidate the CloudFront cache

After deployment, check your email for a temporary password. You'll set a new one on first login.

## Tear Down

```bash
chmod +x teardown.sh
./teardown.sh [region]
```

This finds all `mp-buyer-*` CloudFormation stacks in the region, empties their S3 buckets, and deletes them.

## Project Structure

```
.
├── deploy_all.sh              # Full deployment script
├── teardown.sh                # Stack cleanup script
└── web/sweb/
    ├── template.json          # CloudFormation template
    ├── frontend/
    │   ├── index.html         # SPA frontend
    │   └── js/                # Client-side libraries (Cognito SDK)
    ├── lambda/
    │   └── handler.py         # Main API handler (agreements + products + subscribe)
    ├── lambda-reports/
    │   └── handler.py         # Report generation (Strands agent)
    └── lambda-sync/
        └── handler.py         # Scheduled sync to DynamoDB
```

## API Endpoints

All endpoints (except OPTIONS) require a Cognito JWT in the `Authorization` header.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Health check |
| POST | `/api/agreements/search` | Search agreements |
| GET | `/api/agreements/{id}` | Describe an agreement |
| GET | `/api/agreements/{id}/terms` | Get agreement terms |
| POST | `/api/products/search` | Search Marketplace catalog |
| GET | `/api/products/{id}/options` | List purchase options |
| POST | `/api/subscribe` | Create subscription request |
| POST | `/api/subscribe/accept` | Accept subscription |
| POST | `/api/reports` | Generate a report |
| GET | `/api/reports` | List/download reports |

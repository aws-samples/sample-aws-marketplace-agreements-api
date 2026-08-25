#!/bin/bash
set -e

###############################################################################
# MP-Buyer - Full Deployment Script (Fresh AWS Account)
#
# Deploys the serverless web frontend:
#   - CloudFormation (API Gateway + Lambda + S3 + CloudFront)
#   - Lambda code bundled with latest boto3 (for Discovery API)
#   - Frontend HTML to S3
#   - IAM permissions for Marketplace Agreement + Discovery APIs
#
# Prerequisites:
#   - AWS CLI configured (aws configure)
#   - Python 3.10+ with pip
#
# Usage:
#   chmod +x deploy_all.sh
#   ./deploy_all.sh [region] [username] [email]
#
# Default region: us-east-1
###############################################################################

REGION="${1:-us-east-1}"
RANDOM_SUFFIX=$(( RANDOM % 9000 + 1000 ))
STACK_NAME="mp-buyer-web-${RANDOM_SUFFIX}"
ADMIN_USERNAME="${2:-}"
ADMIN_EMAIL="${3:-}"
PYTHON_CMD="python3"

# Detect python version
if command -v python3.11 &> /dev/null; then
    PYTHON_CMD="python3.11"
elif command -v python3.12 &> /dev/null; then
    PYTHON_CMD="python3.12"
elif command -v python3.10 &> /dev/null; then
    PYTHON_CMD="python3.10"
fi

echo "=========================================="
echo "MP-Buyer - Serverless Web Deployment"
echo "=========================================="
echo ""
echo "Region:  $REGION"
echo "Python:  $PYTHON_CMD"
echo "Stack:   $STACK_NAME"
echo ""

# Prompt for admin user if not provided
if [ -z "$ADMIN_USERNAME" ]; then
    read -p "Admin username: " ADMIN_USERNAME
fi
if [ -z "$ADMIN_EMAIL" ]; then
    read -p "Admin email: " ADMIN_EMAIL
fi

echo "Admin:   $ADMIN_USERNAME ($ADMIN_EMAIL)"
echo ""

# -------------------------------------------------------------------
# Pre-flight checks
# -------------------------------------------------------------------
echo "--- Pre-flight Checks ---"
echo ""

if ! command -v aws &> /dev/null; then
    echo "✗ AWS CLI not found. Install: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
    exit 1
fi
echo "✓ AWS CLI found"

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text 2>/dev/null)
if [ -z "$ACCOUNT_ID" ]; then
    echo "✗ AWS credentials not configured. Run: aws configure"
    exit 1
fi
echo "✓ AWS credentials valid (Account: $ACCOUNT_ID)"

if ! command -v $PYTHON_CMD &> /dev/null; then
    echo "✗ Python 3.10+ not found"
    exit 1
fi
echo "✓ Python: $($PYTHON_CMD --version 2>&1 | awk '{print $2}')"

if ! $PYTHON_CMD -m pip --version &> /dev/null; then
    echo "✗ pip not found for $PYTHON_CMD"
    exit 1
fi
echo "✓ pip available"

echo ""
echo "=========================================="
echo "STEP 1: Deploy CloudFormation Stack"
echo "=========================================="
echo ""

cd web/sweb

aws cloudformation deploy \
  --template-file template.json \
  --stack-name "$STACK_NAME" \
  --region "$REGION" \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    AdminUsername="$ADMIN_USERNAME" \
    AdminEmail="$ADMIN_EMAIL" \
  --no-fail-on-empty-changeset

echo "✓ CloudFormation stack deployed"
echo ""

# Get outputs
API_URL=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='ApiGatewayUrl'].OutputValue" --output text)

CF_URL=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='CloudFrontUrl'].OutputValue" --output text)

BUCKET=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='WebsiteBucketName'].OutputValue" --output text)

LAMBDA_NAME=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='LambdaFunctionName'].OutputValue" --output text)

echo "  API URL:     $API_URL"
echo "  CloudFront:  $CF_URL"
echo "  S3 Bucket:   $BUCKET"
echo "  Lambda:      $LAMBDA_NAME"
echo ""

echo "=========================================="
echo "STEP 2: Deploy Lambda Code"
echo "=========================================="
echo ""

LAMBDA_PKG_DIR="/tmp/mp-buyer-lambda-pkg"
rm -rf "$LAMBDA_PKG_DIR"
mkdir -p "$LAMBDA_PKG_DIR"

echo "Installing boto3 into Lambda package..."
$PYTHON_CMD -m pip install --quiet --target "$LAMBDA_PKG_DIR" boto3

cp lambda/handler.py "$LAMBDA_PKG_DIR/"

cd "$LAMBDA_PKG_DIR"
zip -qr /tmp/mp-buyer-lambda.zip .
cd -

aws lambda update-function-code \
  --function-name "$LAMBDA_NAME" \
  --zip-file fileb:///tmp/mp-buyer-lambda.zip \
  --region "$REGION" > /dev/null

echo "✓ Lambda code deployed (with latest boto3)"
echo ""

# Deploy Reports Lambda
echo "Packaging Reports Lambda..."
REPORTS_PKG_DIR="/tmp/mp-buyer-reports-pkg"
rm -rf "$REPORTS_PKG_DIR"
mkdir -p "$REPORTS_PKG_DIR"

$PYTHON_CMD -m pip install --quiet --target "$REPORTS_PKG_DIR" boto3 strands-agents
cp lambda-reports/handler.py "$REPORTS_PKG_DIR/"

cd "$REPORTS_PKG_DIR"
zip -qr /tmp/mp-buyer-reports.zip .
cd -

REPORTS_LAMBDA_NAME=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='ReportsFunctionName'].OutputValue" --output text)

aws lambda update-function-code \
  --function-name "$REPORTS_LAMBDA_NAME" \
  --zip-file fileb:///tmp/mp-buyer-reports.zip \
  --region "$REGION" > /dev/null

echo "✓ Reports Lambda deployed (with Strands + boto3)"
echo ""

# Deploy Sync Lambda
echo "Packaging Sync Lambda..."
SYNC_PKG_DIR="/tmp/mp-buyer-sync-pkg"
rm -rf "$SYNC_PKG_DIR"
mkdir -p "$SYNC_PKG_DIR"

$PYTHON_CMD -m pip install --quiet --target "$SYNC_PKG_DIR" boto3
cp lambda-sync/handler.py "$SYNC_PKG_DIR/"

cd "$SYNC_PKG_DIR"
zip -qr /tmp/mp-buyer-sync.zip .
cd -

SYNC_LAMBDA_NAME=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='SyncFunctionName'].OutputValue" --output text)

aws lambda update-function-code \
  --function-name "$SYNC_LAMBDA_NAME" \
  --zip-file fileb:///tmp/mp-buyer-sync.zip \
  --region "$REGION" > /dev/null

echo "✓ Sync Lambda deployed"
echo ""

# Trigger initial sync
echo "Running initial data sync..."
aws lambda invoke \
  --function-name "$SYNC_LAMBDA_NAME" \
  --region "$REGION" \
  --payload '{}' \
  --cli-binary-format raw-in-base64-out \
  /tmp/sync-result.json > /dev/null 2>&1

if [ -f /tmp/sync-result.json ]; then
    echo "  $(cat /tmp/sync-result.json)"
fi
echo "✓ Initial sync complete"
echo ""

echo "=========================================="
echo "STEP 3: Generate Frontend Auth Config"
echo "=========================================="
echo ""

USER_POOL_ID=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='UserPoolId'].OutputValue" --output text)

USER_POOL_CLIENT_ID=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='UserPoolClientId'].OutputValue" --output text)

# Generate config.js for the frontend
cat > frontend/config.js << CONFIGEOF
// Auto-generated by deploy_all.sh
window.APP_CONFIG = {
    region: "${REGION}",
    userPoolId: "${USER_POOL_ID}",
    userPoolClientId: "${USER_POOL_CLIENT_ID}",
    apiUrl: "${API_URL}"
};
CONFIGEOF

echo "  User Pool ID:     $USER_POOL_ID"
echo "  Client ID:        $USER_POOL_CLIENT_ID"
echo "✓ Frontend config.js generated"
echo ""

echo "=========================================="
echo "STEP 4: Add IAM Permissions"
echo "=========================================="
echo ""

ROLE_NAME="mp-buyer-lambda-${STACK_NAME}"

aws iam put-role-policy \
  --role-name "$ROLE_NAME" \
  --policy-name "MarketplaceDiscoveryAccess" \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [
      {
        "Effect": "Allow",
        "Action": [
          "aws-marketplace:GetProduct",
          "aws-marketplace:SearchListings",
          "aws-marketplace:ListPurchaseOptions",
          "aws-marketplace:GetListing",
          "aws-marketplace:GetOffer",
          "aws-marketplace:GetOfferTerms",
          "aws-marketplace:SearchFacets"
        ],
        "Resource": "*"
      }
    ]
  }'

aws iam put-role-policy \
  --role-name "$ROLE_NAME" \
  --policy-name "MarketplaceSubscribeAccess" \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [
      {
        "Effect": "Allow",
        "Action": [
          "aws-marketplace:CreateAgreementRequest",
          "aws-marketplace:AcceptAgreementRequest",
          "aws-marketplace:GetAgreementRequest"
        ],
        "Resource": "*"
      }
    ]
  }'

echo "✓ IAM permissions added (Discovery + Subscribe)"
echo ""

echo "=========================================="
echo "STEP 5: Upload Frontend to S3"
echo "=========================================="
echo ""

aws s3 sync frontend/ "s3://$BUCKET/" \
  --region "$REGION" \
  --delete

echo "✓ Frontend uploaded"
echo ""

echo "=========================================="
echo "STEP 6: Invalidate CloudFront Cache"
echo "=========================================="
echo ""

CF_DIST_ID=$(aws cloudformation describe-stack-resources \
  --stack-name "$STACK_NAME" --region "$REGION" \
  --query "StackResources[?LogicalResourceId=='CloudFrontDistribution'].PhysicalResourceId" \
  --output text)

if [ -n "$CF_DIST_ID" ]; then
    aws cloudfront create-invalidation \
      --distribution-id "$CF_DIST_ID" \
      --paths "/*" > /dev/null 2>&1
    echo "✓ Cache invalidated ($CF_DIST_ID)"
else
    echo "⚠ CloudFront distribution ID not found"
fi

cd ../..

echo ""
echo "=========================================="
echo "STEP 7: Verify"
echo "=========================================="
echo ""

sleep 5
HEALTH=$(curl -s "$API_URL/api/health" 2>/dev/null)
if echo "$HEALTH" | grep -q "healthy"; then
    echo "✓ API is healthy"
else
    echo "⚠ API may need a few seconds to warm up"
fi

echo ""
echo "=========================================="
echo "✅ DEPLOYMENT COMPLETE"
echo "=========================================="
echo ""
echo "┌─────────────────────────────────────────────────────────┐"
echo "│  Website:  $CF_URL"
echo "│  API:      $API_URL"
echo "│  Account:  $ACCOUNT_ID"
echo "│  Region:   $REGION"
echo "└─────────────────────────────────────────────────────────┘"
echo ""
echo "Features:"
echo "  ✓ Cognito authentication (login required)"
echo "  ✓ Agreements - View and manage marketplace subscriptions"
echo "  ✓ Discover Products - Search marketplace and subscribe"
echo ""
echo "First login:"
echo "  Email: $ADMIN_EMAIL"
echo "  Password: Check your email for temporary password"
echo "  (You'll be prompted to set a new password on first login)"
echo ""
echo "Note: CloudFront takes 5-10 minutes to fully propagate."
echo ""
echo "To tear down:"
echo "  aws s3 rm s3://$BUCKET --recursive"
echo "  aws cloudformation delete-stack --stack-name $STACK_NAME --region $REGION"
echo ""

# Save deployment info
cat > deployment_info.env << EOF
export STACK_NAME="$STACK_NAME"
export REGION="$REGION"
export ACCOUNT_ID="$ACCOUNT_ID"
export API_URL="$API_URL"
export CF_URL="$CF_URL"
export BUCKET="$BUCKET"
export LAMBDA_NAME="$LAMBDA_NAME"
export CF_DIST_ID="$CF_DIST_ID"
export ROLE_NAME="$ROLE_NAME"
EOF

echo "Deployment info saved to deployment_info.env"
echo ""

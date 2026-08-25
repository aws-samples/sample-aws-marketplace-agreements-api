#!/bin/bash
###############################################################################
# Tear down all MP-Buyer stacks in the account
#
# Usage:
#   chmod +x teardown.sh
#   ./teardown.sh [region]
###############################################################################

REGION="${1:-us-east-1}"

echo "=========================================="
echo "MP-Buyer - Tear Down"
echo "=========================================="
echo ""

# Find all mp-buyer stacks
STACKS=$(aws cloudformation list-stacks \
  --region "$REGION" \
  --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE \
  --query "StackSummaries[?contains(StackName,'mp-buyer')].StackName" \
  --output text)

if [ -z "$STACKS" ]; then
    echo "No mp-buyer stacks found in $REGION"
    exit 0
fi

echo "Found stacks:"
for STACK in $STACKS; do
    echo "  - $STACK"
done
echo ""

read -p "Delete ALL of the above stacks? (yes/no): " CONFIRM
if [ "$CONFIRM" != "yes" ]; then
    echo "Cancelled."
    exit 0
fi

for STACK in $STACKS; do
    echo ""
    echo "--- Deleting: $STACK ---"

    # Get bucket name
    BUCKET=$(aws cloudformation describe-stacks \
      --stack-name "$STACK" --region "$REGION" \
      --query "Stacks[0].Outputs[?OutputKey=='WebsiteBucketName'].OutputValue" \
      --output text 2>/dev/null)

    # Empty bucket if it exists
    if [ -n "$BUCKET" ] && [ "$BUCKET" != "None" ]; then
        echo "  Emptying S3 bucket: $BUCKET"
        aws s3 rm "s3://$BUCKET" --recursive --region "$REGION" 2>/dev/null
    fi

    # Delete stack
    echo "  Deleting CloudFormation stack..."
    aws cloudformation delete-stack --stack-name "$STACK" --region "$REGION"
    echo "  ✓ Delete initiated for $STACK"
done

echo ""
echo "=========================================="
echo "✓ Tear down initiated for all stacks"
echo "=========================================="
echo ""
echo "Stacks are deleting in the background."
echo "Check status: aws cloudformation list-stacks --region $REGION --stack-status-filter DELETE_IN_PROGRESS"
echo ""

#!/usr/bin/env bash
# Copyright 2026 The Apigee Entitlement Tracker Authors
# SPDX-License-Identifier: Apache-2.0
# ==============================================================================
# redeploy_cloudrun.sh
#
# Builds container from current local source and redeploys to Google Cloud Run.
# Automatically detects and mounts Secret Manager OAuth secrets if present.
# ==============================================================================

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

# ANSI colors
BOLD='\033[1m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

# Default configuration
PROJECT_ID="${GCP_PROJECT:-<YOUR-PROJECT-ID>}"
REGION="${GCP_REGION:-<YOUR-REGION>}"
SERVICE_NAME="${CLOUD_RUN_SERVICE:-apigee-entitlement-tracker}"
SA_EMAIL="${CLOUD_RUN_SA:-}"
REQUIRE_IAM_AUTH=false
SECRET_CLIENT_ID="apigee-tracker-oauth-client-id"
SECRET_CLIENT_SECRET="apigee-tracker-oauth-client-secret"

print_usage() {
  echo -e "${BOLD}Usage:${NC} $(basename "$0") [OPTIONS]"
  echo ""
  echo -e "${BOLD}Options:${NC}"
  echo -e "  --project <PROJECT_ID>    GCP Project ID (default: ${PROJECT_ID})"
  echo -e "  --region <REGION>         GCP Region (default: ${REGION})"
  echo -e "  --service <SERVICE_NAME>  Cloud Run service name (default: ${SERVICE_NAME})"
  echo -e "  --sa <SA_EMAIL>           Runtime Service Account (auto-detects existing service SA or default compute SA)"
  echo -e "  --require-iam-auth        Deploy with --no-allow-unauthenticated (for IAP / IAM-gated access)"
  echo -e "  -h, --help                Show this help message"
  echo ""
  echo -e "${BOLD}Example:${NC}"
  echo -e "  ./scripts/redeploy_cloudrun.sh --project my-gcp-project --region europe-west1"
}

require_arg() {
  if [[ $# -lt 2 || -z "${2:-}" ]]; then
    echo -e "${RED}Error: Option '$1' requires a non-empty value.${NC}" >&2
    exit 1
  fi
}

# Parse flags
while [[ $# -gt 0 ]]; do
  case "$1" in
    --project)
      require_arg "$@"
      PROJECT_ID="$2"
      shift 2
      ;;
    --region)
      require_arg "$@"
      REGION="$2"
      shift 2
      ;;
    --service)
      require_arg "$@"
      SERVICE_NAME="$2"
      shift 2
      ;;
    --sa)
      require_arg "$@"
      SA_EMAIL="$2"
      shift 2
      ;;
    --require-iam-auth)
      REQUIRE_IAM_AUTH=true
      shift
      ;;
    -h|--help)
      print_usage
      exit 0
      ;;
    *)
      echo -e "${RED}Unknown option: $1${NC}"
      print_usage
      exit 1
      ;;
  esac
done

if [[ -z "${PROJECT_ID}" || "${PROJECT_ID}" == "<YOUR-PROJECT-ID>" ]]; then
  echo -e "${RED}Error: GCP Project ID is required. Pass --project <PROJECT_ID> or set GCP_PROJECT.${NC}" >&2
  exit 1
fi
if [[ -z "${REGION}" || "${REGION}" == "<YOUR-REGION>" ]]; then
  echo -e "${RED}Error: GCP Region is required. Pass --region <REGION> or set GCP_REGION.${NC}" >&2
  exit 1
fi
# 1. Verify gcloud authentication
if ! command -v gcloud &>/dev/null; then
  echo -e "${RED}Error: 'gcloud' CLI is not installed or not in PATH.${NC}"
  exit 1
fi

if [[ -z "${SA_EMAIL}" ]]; then
  SA_EMAIL=$(gcloud run services describe "${SERVICE_NAME}" \
    --project="${PROJECT_ID}" \
    --region="${REGION}" \
    --format="value(spec.template.spec.serviceAccountName)" 2>/dev/null || echo "")
  if [[ -z "${SA_EMAIL}" ]]; then
    PROJECT_NUMBER=$(gcloud projects describe "${PROJECT_ID}" --format="value(projectNumber)" 2>/dev/null || echo "")
    if [[ -n "${PROJECT_NUMBER}" ]]; then
      SA_EMAIL="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
    fi
  fi
fi

echo -e "\n${BOLD}${CYAN}=== Apigee Entitlement Tracker - Cloud Run Deployment ===${NC}"
echo -e "  ${BOLD}Project:${NC}         ${PROJECT_ID}"
echo -e "  ${BOLD}Region:${NC}          ${REGION}"
echo -e "  ${BOLD}Service:${NC}         ${SERVICE_NAME}"
echo -e "  ${BOLD}Service Account:${NC} ${SA_EMAIL:-<Cloud Run Default>}"
echo ""

ACTIVE_ACCOUNT=$(gcloud config get-value account 2>/dev/null || echo "")
if [[ -z "${ACTIVE_ACCOUNT}" ]]; then
  echo -e "${RED}Error: No active gcloud account detected. Run 'gcloud auth login'.${NC}"
  exit 1
fi
echo -e "${GREEN}✓ Authenticated as:${NC} ${ACTIVE_ACCOUNT}"

# 2. Check if Secret Manager secrets exist
SECRETS_FLAG=""
if gcloud secrets describe "${SECRET_CLIENT_ID}" --project="${PROJECT_ID}" &>/dev/null && \
   gcloud secrets describe "${SECRET_CLIENT_SECRET}" --project="${PROJECT_ID}" &>/dev/null; then
  echo -e "${GREEN}✓ OAuth secrets detected in Secret Manager.${NC} Mounting into Cloud Run..."
  SECRETS_FLAG="--set-secrets=GOOGLE_CLIENT_ID=${SECRET_CLIENT_ID}:latest,GOOGLE_CLIENT_SECRET=${SECRET_CLIENT_SECRET}:latest"
else
  echo -e "${YELLOW}ℹ OAuth secrets not detected in Secret Manager. Existing revision secrets (if any) will be preserved.${NC}"
fi

# 3. Deploy Cloud Run service from source
echo -e "\n${BOLD}${BLUE}==> Building and deploying Cloud Run service from source...${NC}"

AUTH_FLAG="--allow-unauthenticated"
if [[ "${REQUIRE_IAM_AUTH}" == true ]]; then
  AUTH_FLAG="--no-allow-unauthenticated"
fi

DEPLOY_CMD=(
  gcloud run deploy "${SERVICE_NAME}"
  --source .
  --project="${PROJECT_ID}"
  --region="${REGION}"
  --session-affinity
  --memory="1Gi"
  --cpu="1"
  --port=8080
  "${AUTH_FLAG}"
  --quiet
)

if [[ -n "${SA_EMAIL}" ]]; then
  DEPLOY_CMD+=(--service-account="${SA_EMAIL}")
fi

if [[ -n "${SECRETS_FLAG}" ]]; then
  DEPLOY_CMD+=("${SECRETS_FLAG}")
fi

"${DEPLOY_CMD[@]}"

# 4. Fetch service details
SERVICE_URL=$(gcloud run services describe "${SERVICE_NAME}" \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --format="value(status.url)" 2>/dev/null || echo "")

LATEST_REVISION=$(gcloud run services describe "${SERVICE_NAME}" \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --format="value(status.latestReadyRevisionName)" 2>/dev/null || echo "")

echo -e "\n${BOLD}${GREEN}==============================================================${NC}"
echo -e "${BOLD}${GREEN}✓ Redeployment Successful!${NC}"
echo -e "  ${BOLD}Service URL:${NC}     ${SERVICE_URL}"
echo -e "  ${BOLD}Revision:${NC}        ${LATEST_REVISION}"
echo -e "  ${BOLD}Region:${NC}          ${REGION}"
echo -e "  ${BOLD}Project:${NC}         ${PROJECT_ID}"
echo -e "${BOLD}${GREEN}==============================================================${NC}\n"

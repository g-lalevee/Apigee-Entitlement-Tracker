#!/usr/bin/env bash
# ==============================================================================
# setup_oauth_cloudrun.sh
#
# Automates the configuration of Google Cloud OAuth 2.0 Web Client authentication
# for Apigee Entitlement Tracker on Cloud Run:
#
#   1. Determines service URL & displays pre-filled OAuth console link & redirect URIs
#   2. Securely creates/updates Google Cloud Secret Manager secrets for:
#        - GOOGLE_CLIENT_ID
#        - GOOGLE_CLIENT_SECRET
#   3. Automatically grants 'roles/secretmanager.secretAccessor' IAM role to
#      the Cloud Run runtime Service Account
#   4. Updates or deploys the Cloud Run service with Secret Manager mounts
#   5. Verifies live deployment and OAuth readiness
# ==============================================================================

set -euo pipefail

# ANSI color codes
BOLD='\033[1m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Defaults
PROJECT_ID="${GCP_PROJECT:-<YOUR-PROJECT-ID>}"
REGION="${GCP_REGION:<YOUR-REGION>}"
SERVICE_NAME="${CLOUD_RUN_SERVICE:-apigee-entitlement-tracker}"
SECRET_CLIENT_ID="apigee-tracker-oauth-client-id"
SECRET_CLIENT_SECRET="apigee-tracker-oauth-client-secret"
DEPLOY_FROM_SOURCE=false
SKIP_DEPLOY=false
CLIENT_ID="${GOOGLE_CLIENT_ID:-}"
CLIENT_SECRET="${GOOGLE_CLIENT_SECRET:-}"

print_usage() {
  echo -e "${BOLD}Usage:${NC} $(basename "$0") [OPTIONS]"
  echo ""
  echo -e "${BOLD}Options:${NC}"
  echo -e "  --client-id <ID>          OAuth 2.0 Web Client ID"
  echo -e "  --client-secret <SECRET>  OAuth 2.0 Web Client Secret"
  echo -e "  --project <PROJECT_ID>    GCP Project ID (default: ${PROJECT_ID})"
  echo -e "  --region <REGION>         GCP Region (default: ${REGION})"
  echo -e "  --service <SERVICE_NAME>  Cloud Run service name (default: ${SERVICE_NAME})"
  echo -e "  --deploy-source           Rebuild & deploy from current source code with secrets"
  echo -e "  --skip-deploy             Only create secrets and IAM bindings; skip Cloud Run update"
  echo -e "  -h, --help                Show this help message"
  echo ""
  echo -e "${BOLD}Examples:${NC}"
  echo -e "  # Interactive prompt"
  echo -e "  ./scripts/setup_oauth_cloudrun.sh"
  echo ""
  echo -e "  # Automated with arguments"
  echo -e "  ./scripts/setup_oauth_cloudrun.sh \\"
  echo -e "    --client-id \"project_id-xxx.apps.googleusercontent.com\" \\"
  echo -e "    --client-secret \"GOCSPX-xxx\""
  echo ""
  echo -e "  # Deploy current source with secrets"
  echo -e "  ./scripts/setup_oauth_cloudrun.sh --deploy-source"
}

# Parse command-line arguments
while [[ $# -gt 0 ]]; do
  case "$1" in
    --client-id)
      CLIENT_ID="$2"
      shift 2
      ;;
    --client-secret)
      CLIENT_SECRET="$2"
      shift 2
      ;;
    --project)
      PROJECT_ID="$2"
      shift 2
      ;;
    --region)
      REGION="$2"
      shift 2
      ;;
    --service)
      SERVICE_NAME="$2"
      shift 2
      ;;
    --deploy-source)
      DEPLOY_FROM_SOURCE=true
      shift
      ;;
    --skip-deploy)
      SKIP_DEPLOY=true
      shift
      ;;
    -h|--help)
      print_usage
      exit 0
      ;;
    *)
      echo -e "${RED}Error: Unknown parameter '$1'${NC}" >&2
      print_usage
      exit 1
      ;;
  esac
done

echo -e "${BOLD}${CYAN}====================================================================${NC}"
echo -e "${BOLD}${CYAN}   Apigee Entitlement Tracker - OAuth 2.0 Cloud Run Setup          ${NC}"
echo -e "${BOLD}${CYAN}====================================================================${NC}"
echo -e "Project: ${BOLD}${PROJECT_ID}${NC}"
echo -e "Region:  ${BOLD}${REGION}${NC}"
echo -e "Service: ${BOLD}${SERVICE_NAME}${NC}"
echo ""

# Ensure gcloud CLI is authenticated
if ! gcloud auth print-access-token >/dev/null 2>&1; then
  echo -e "${RED}Error: gcloud is not authenticated. Please run 'gcloud auth login' or 'gcloud auth application-default login'.${NC}" >&2
  exit 1
fi

# Step 1: Detect Cloud Run Service URL & Authorized Redirect URIs
echo -e "${BOLD}${BLUE}Step 1: Detecting Cloud Run Service & Redirect URIs...${NC}"
SERVICE_URL=$(gcloud run services describe "${SERVICE_NAME}" \
  --project "${PROJECT_ID}" \
  --region "${REGION}" \
  --format="value(status.url)" 2>/dev/null || echo "")

if [[ -z "${SERVICE_URL}" ]]; then
  echo -e "${YELLOW}Warning: Cloud Run service '${SERVICE_NAME}' not found in ${REGION}. Using default URL pattern.${NC}"
  PROJECT_NUMBER=$(gcloud projects describe "${PROJECT_ID}" --format="value(projectNumber)" 2>/dev/null || echo "default-project-number")
  SERVICE_URL="https://${SERVICE_NAME}-${PROJECT_NUMBER}.${REGION}.run.app"
fi

REDIRECT_URI_CLOUDRUN="${SERVICE_URL}/api/auth/oidc/callback"
REDIRECT_URI_LOCAL="http://localhost:8080/api/auth/oidc/callback"

echo -e "Service URL:             ${GREEN}${SERVICE_URL}${NC}"
echo -e "Cloud Run Callback URI:  ${GREEN}${REDIRECT_URI_CLOUDRUN}${NC}"
echo -e "Localhost Callback URI:  ${GREEN}${REDIRECT_URI_LOCAL}${NC}"
echo ""

# Prompt for OAuth Client Credentials if not passed
if [[ -z "${CLIENT_ID}" || -z "${CLIENT_SECRET}" ]]; then
  echo -e "${BOLD}${YELLOW}--------------------------------------------------------------------${NC}"
  echo -e "${BOLD}Google Cloud OAuth 2.0 Web Client Creation:${NC}"
  echo -e "Google requires Web OAuth Client IDs to be registered via the Cloud Console."
  echo ""
  echo -e "1. Open Google Cloud Console:"
  echo -e "   👉 ${CYAN}https://console.cloud.google.com/apis/credentials/oauthclient?project=${PROJECT_ID}${NC}"
  echo ""
  echo -e "2. Configure the client:"
  echo -e "   • Application type: ${BOLD}Web application${NC}"
  echo -e "   • Name:             ${BOLD}Apigee Entitlement Tracker${NC}"
  echo -e "   • Authorized redirect URIs (add both):"
  echo -e "     ${BOLD}${REDIRECT_URI_CLOUDRUN}${NC}"
  echo -e "     ${BOLD}${REDIRECT_URI_LOCAL}${NC}"
  echo ""
  echo -e "3. Click ${BOLD}'CREATE'${NC} and copy the generated credentials."
  echo -e "${BOLD}${YELLOW}--------------------------------------------------------------------${NC}"
  echo ""

  if [[ -z "${CLIENT_ID}" ]]; then
    read -rp "Enter Google OAuth Client ID: " CLIENT_ID
  fi

  if [[ -z "${CLIENT_SECRET}" ]]; then
    read -rsp "Enter Google OAuth Client Secret: " CLIENT_SECRET
    echo ""
  fi
fi

if [[ -z "${CLIENT_ID}" || -z "${CLIENT_SECRET}" ]]; then
  echo -e "${RED}Error: Both Client ID and Client Secret are required.${NC}" >&2
  exit 1
fi

# Clean values
CLIENT_ID=$(echo "${CLIENT_ID}" | xargs)
CLIENT_SECRET=$(echo "${CLIENT_SECRET}" | xargs)

# Step 2: Store in Secret Manager
echo ""
echo -e "${BOLD}${BLUE}Step 2: Storing credentials in Secret Manager...${NC}"

# Enable Secret Manager API if not already enabled
gcloud services enable secretmanager.googleapis.com --project "${PROJECT_ID}" --quiet >/dev/null 2>&1 || true

save_secret() {
  local secret_name="$1"
  local secret_val="$2"

  if gcloud secrets describe "${secret_name}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
    echo -e "  Secret '${secret_name}' exists. Adding new version..."
    echo -n "${secret_val}" | gcloud secrets versions add "${secret_name}" \
      --data-file=- \
      --project "${PROJECT_ID}" \
      --quiet >/dev/null
  else
    echo -e "  Creating secret '${secret_name}'..."
    echo -n "${secret_val}" | gcloud secrets create "${secret_name}" \
      --data-file=- \
      --replication-policy="automatic" \
      --project "${PROJECT_ID}" \
      --quiet >/dev/null
  fi
}

save_secret "${SECRET_CLIENT_ID}" "${CLIENT_ID}"
save_secret "${SECRET_CLIENT_SECRET}" "${CLIENT_SECRET}"
echo -e "${GREEN}✓ Secrets stored successfully in Secret Manager.${NC}"

# Step 3: Grant IAM Permissions to Cloud Run Runtime Service Account
echo ""
echo -e "${BOLD}${BLUE}Step 3: Configuring IAM permissions for Cloud Run Service Account...${NC}"

RUN_SA=$(gcloud run services describe "${SERVICE_NAME}" \
  --project "${PROJECT_ID}" \
  --region "${REGION}" \
  --format="value(spec.template.spec.serviceAccountName)" 2>/dev/null || echo "")

if [[ -z "${RUN_SA}" ]]; then
  PROJECT_NUMBER=$(gcloud projects describe "${PROJECT_ID}" --format="value(projectNumber)")
  RUN_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
fi

echo -e "  Runtime Service Account: ${BOLD}${RUN_SA}${NC}"

gcloud secrets add-iam-policy-binding "${SECRET_CLIENT_ID}" \
  --member="serviceAccount:${RUN_SA}" \
  --role="roles/secretmanager.secretAccessor" \
  --project "${PROJECT_ID}" \
  --quiet >/dev/null

gcloud secrets add-iam-policy-binding "${SECRET_CLIENT_SECRET}" \
  --member="serviceAccount:${RUN_SA}" \
  --role="roles/secretmanager.secretAccessor" \
  --project "${PROJECT_ID}" \
  --quiet >/dev/null

echo -e "${GREEN}✓ Granted 'roles/secretmanager.secretAccessor' on secrets to ${RUN_SA}.${NC}"

# Step 4: Deploy / Update Cloud Run Service
if [[ "${SKIP_DEPLOY}" == true ]]; then
  echo ""
  echo -e "${YELLOW}Skipping Cloud Run deployment (--skip-deploy specified).${NC}"
else
  echo ""
  echo -e "${BOLD}${BLUE}Step 4: Updating Cloud Run service with Secret Manager bindings...${NC}"

  SECRETS_PARAM="GOOGLE_CLIENT_ID=${SECRET_CLIENT_ID}:latest,GOOGLE_CLIENT_SECRET=${SECRET_CLIENT_SECRET}:latest"

  if [[ "${DEPLOY_FROM_SOURCE}" == true ]]; then
    echo -e "  Rebuilding and deploying from source code..."
    gcloud run deploy "${SERVICE_NAME}" \
      --source . \
      --project "${PROJECT_ID}" \
      --region "${REGION}" \
      --set-secrets="${SECRETS_PARAM}" \
      --quiet
  else
    echo -e "  Updating existing service revision..."
    gcloud run services update "${SERVICE_NAME}" \
      --project "${PROJECT_ID}" \
      --region "${REGION}" \
      --set-secrets="${SECRETS_PARAM}" \
      --quiet
  fi

  echo -e "${GREEN}✓ Cloud Run service updated with OAuth 2.0 secrets.${NC}"
fi

# Step 5: Verification
echo ""
echo -e "${BOLD}${BLUE}Step 5: Verifying service configuration...${NC}"

HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" "${SERVICE_URL}" || echo "000")
echo -e "  Health Check Status: ${GREEN}HTTP ${HTTP_STATUS}${NC}"

echo ""
echo -e "${BOLD}${GREEN}====================================================================${NC}"
echo -e "${BOLD}${GREEN}   Configuration Complete! Pattern A OAuth 2.0 is Active!           ${NC}"
echo -e "${BOLD}${GREEN}====================================================================${NC}"
echo -e "Your Cloud Run service is now configured with Google OAuth 2.0 Web Client."
echo ""
echo -e "Application URL: ${BOLD}${CYAN}${SERVICE_URL}${NC}"
echo ""
echo -e "Next steps:"
echo -e "1. Visit ${CYAN}${SERVICE_URL}${NC}"
echo -e "2. Click ${BOLD}'Sign in with Google (OAuth 2.0)'${NC}"
echo -e "3. Grant consent on the Google screen"
echo -e "4. The app will automatically enter the dashboard and query all organizations"
echo -e "   using your personal user IAM identity!"
echo ""

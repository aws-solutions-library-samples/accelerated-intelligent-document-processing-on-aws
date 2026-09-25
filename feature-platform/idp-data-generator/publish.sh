#!/usr/bin/env bash
# Publish the IDP Data Generator extension end-to-end, then print the Launch URL.
#
# Wraps `idp-feature-cli publish` (the public Feature Platform SDK). That command
# already: validates feature.yaml, runs the UI buildCommand, runs the agentSource
# packageCommand (package_agent_source.sh), runs `sam build`/`sam package` on
# template.yaml, uploads all artifacts, and prints a Launch Stack URL.
#
# This wrapper just enforces the prerequisites and gives one command to run.
#
# Usage:
#   ./extension/publish.sh <bucket-basename> [region] [main-stack-name]
# Example (dev, public-read so the Launch URL works without a bucket policy):
#   ./extension/publish.sh my-seller-bucket us-east-1 IDP-dev-stack
#
# Region is appended to the bucket basename by the CLI: my-seller-bucket-us-east-1.
set -euo pipefail

BUCKET_BASENAME="${1:?Usage: publish.sh <bucket-basename> [region] [main-stack-name]}"
REGION="${2:-us-east-1}"
MAIN_STACK="${3:-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- Prerequisites -----------------------------------------------------------
command -v idp-feature-cli >/dev/null 2>&1 || {
  echo "ERROR: idp-feature-cli not found. Install the SDK from the local checkout:" >&2
  echo "         pip install -e lib/idp_feature_sdk      # from the repo root" >&2
  echo "       (or:  pip install -e '.[extension]'  from the repo root)" >&2
  echo "       Do NOT 'pip install idp-feature-sdk' — first-party packages are" >&2
  echo "       not published to PyPI. See docs/dependency-confusion.md." >&2
  exit 1
}
command -v sam >/dev/null 2>&1 || {
  echo "ERROR: AWS SAM CLI ('sam') not found — required to package template.yaml." >&2
  exit 1
}

# --- Publish -----------------------------------------------------------------
# --public so the printed Launch Stack URL works without extra bucket policy.
echo "==> Publishing idp-data-generator → ${BUCKET_BASENAME}-${REGION}"
idp-feature-cli publish "${SCRIPT_DIR}" \
  --bucket-basename "${BUCKET_BASENAME}" \
  --region "${REGION}" \
  --public

echo ""
echo "==> Done. The Launch Stack URL above has a placeholder MAINSTACKNAME."
if [ -n "${MAIN_STACK}" ]; then
  echo "    Substitute MAINSTACKNAME=${MAIN_STACK} in that URL and open it to install,"
  echo "    or deploy from the CLI once you know the template URL:"
  echo ""
  echo "    aws cloudformation create-stack \\"
  echo "      --stack-name idp-feature-idp-data-generator \\"
  echo "      --capabilities CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND \\"
  # FeatureBucket is declared with no Default, so a command listing only
  # MainStackName is rejected with "Parameters: [FeatureBucket] must have values"
  # and creates nothing. The installer supplies it; a hand-rolled command must too.
  echo "      --parameters ParameterKey=MainStackName,ParameterValue=${MAIN_STACK} \\"
  echo "                   ParameterKey=FeatureBucket,ParameterValue=${BUCKET_BASENAME}-${REGION} \\"
  echo "      --template-url <the template.yaml URL printed above> --region ${REGION}"
fi

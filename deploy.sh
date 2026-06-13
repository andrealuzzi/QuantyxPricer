#!/bin/bash
set -euo pipefail

RESOURCE_GROUP="appsvc_linux_switzerlandnorth_premium"
APP_NAME="Lux-Pricer"

ensure_app_running() {
  local max_attempts=12
  local wait_seconds=5

  echo "Starting app..."
  az webapp start --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" > /dev/null

  for ((attempt=1; attempt<=max_attempts; attempt++)); do
    local state
    state=$(az webapp show --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" --query state -o tsv)

    if [[ "$state" == "QuotaExceeded" ]]; then
      echo "App state is QuotaExceeded."
      echo "The current App Service plan has exhausted Free-tier quota."
      echo "Scale up the plan (for example to B1) or wait for quota reset before deploying."
      return 1
    fi

    if [[ "$state" == "Running" ]]; then
      echo "App state is Running."
      return 0
    fi

    echo "Waiting for app to be Running (attempt ${attempt}/${max_attempts}, current state: ${state})..."
    sleep "$wait_seconds"
  done

  echo "App did not reach Running state in time."
  return 1
}

ensure_app_running

echo "Configuring startup command for Python FastAPI..."
az webapp config set \
  --name "$APP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --startup-file "uvicorn api.main:app --host 0.0.0.0 --port 8000" > /dev/null

echo "Ensuring remote build is enabled..."
az webapp config appsettings set \
  --name "$APP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --settings SCM_DO_BUILD_DURING_DEPLOYMENT=true > /dev/null

echo "Creating source-only zip package (excluding __pycache__, .venv, and local artifacts)..."
rm -f deploy.zip
zip -r deploy.zip . \
  --exclude "*.zip" \
  --exclude ".git/*" \
  --exclude ".vscode/*" \
  --exclude ".venv/*" \
  --exclude ".venv/**" \
  --exclude "__pycache__/*" \
  --exclude "__pycache__/**" \
  --exclude "*.pyc" \
  --exclude ".env" \
  --exclude ".DS_Store" \
  --exclude "website/node_modules/*" \
  --exclude "website/node_modules/**" \
  --exclude "website/dist/*" \
  --exclude "website/dist/**"

echo "Deploying..."
deploy_attempts=3
for ((attempt=1; attempt<=deploy_attempts; attempt++)); do
  if az webapp deploy \
      --name "$APP_NAME" \
      --resource-group "$RESOURCE_GROUP" \
      --src-path deploy.zip \
      --type zip \
      --async false; then
    echo "Deployment succeeded."
    break
  fi

  if [[ "$attempt" -eq "$deploy_attempts" ]]; then
    echo "Deployment failed after ${deploy_attempts} attempts."
    exit 1
  fi

  echo "Deployment attempt ${attempt} failed. Ensuring app is running before retry..."
  ensure_app_running
done

echo "Cleaning up..."
rm deploy.zip

echo "Done! App URL: https://$(az webapp show --name $APP_NAME --resource-group $RESOURCE_GROUP --query defaultHostName -o tsv)"

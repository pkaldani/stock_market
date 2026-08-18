#!/usr/bin/env bash
# Build the three Docker images (api/engine/frontend) from the repo-root
# Dockerfile, load them into a local kind cluster, and helm-upgrade-install
# the trading-floor chart against it.
#
# Usage:
#   scripts/deploy-kind.sh [options]
#
# Options:
#   --cluster NAME     kind cluster name (default: kind)
#   --namespace NAME   k8s namespace (default: trading-floor)
#   --release NAME     helm release name (default: trading-floor)
#   --tag TAG          image tag (default: current git short SHA)
#   --env-file PATH    .env file to read secrets from (default: repo root .env)
#   --skip-build       reuse already-built local images for --tag
#   --skip-load        don't `kind load` images (assumes already loaded)
#   --skip-secret      don't create/update the k8s Secret from --env-file
#   --dry-run          render the helm install without applying it
#   -h, --help         show this help
#
# Extra args after `--` are passed straight through to `helm upgrade`, e.g.:
#   scripts/deploy-kind.sh -- --set frontend.replicaCount=1
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CHART_DIR="${REPO_ROOT}/6_mcp/charts/trading-floor"

CLUSTER="kind"
NAMESPACE="trading-floor"
RELEASE="trading-floor"
TAG=""
ENV_FILE="${REPO_ROOT}/.env"
SKIP_BUILD=false
SKIP_LOAD=false
SKIP_SECRET=false
DRY_RUN=false
HELM_EXTRA_ARGS=()

# Keys the app reads from the environment (see CLAUDE.md's "Environment
# variables" section and values.yaml's `secrets.values`).
SECRET_KEYS=(
  ALPACA_API_KEY ALPACA_SECRET_KEY
  OPENAI_API_KEY DEEPSEEK_API_KEY GOOGLE_API_KEY GROK_API_KEY OPENROUTER_API_KEY
  TAVILY_API_KEY
  PUSHOVER_USER PUSHOVER_TOKEN
)

log() { printf '\033[1;36m==>\033[0m %s\n' "$1"; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$1" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --cluster) CLUSTER="$2"; shift 2 ;;
    --namespace) NAMESPACE="$2"; shift 2 ;;
    --release) RELEASE="$2"; shift 2 ;;
    --tag) TAG="$2"; shift 2 ;;
    --env-file) ENV_FILE="$2"; shift 2 ;;
    --skip-build) SKIP_BUILD=true; shift ;;
    --skip-load) SKIP_LOAD=true; shift ;;
    --skip-secret) SKIP_SECRET=true; shift ;;
    --dry-run) DRY_RUN=true; shift ;;
    -h|--help) awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "${BASH_SOURCE[0]}"; exit 0 ;;
    --) shift; HELM_EXTRA_ARGS=("$@"); break ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done

for bin in docker kind kubectl helm; do
  command -v "$bin" >/dev/null 2>&1 || die "'$bin' is required but not on PATH"
done

kind get clusters 2>/dev/null | grep -qx "$CLUSTER" \
  || die "no kind cluster named '$CLUSTER' (create one with: kind create cluster --name $CLUSTER)"

KUBE_CONTEXT="kind-${CLUSTER}"
kubectl config get-contexts -o name | grep -qx "$KUBE_CONTEXT" \
  || die "kubeconfig has no context '$KUBE_CONTEXT' — is kind's kubeconfig merged?"

if [[ -z "$TAG" ]]; then
  TAG="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo dev)"
  git -C "$REPO_ROOT" diff --quiet 2>/dev/null || TAG="${TAG}-dirty"
fi

IMAGES=(api engine frontend)

if [[ "$SKIP_BUILD" == false ]]; then
  for target in "${IMAGES[@]}"; do
    log "building traders-${target}:${TAG}"
    docker build --target "$target" -t "traders-${target}:${TAG}" -f "${REPO_ROOT}/Dockerfile" "$REPO_ROOT"
  done
else
  log "skipping build (--skip-build), expecting traders-{api,engine,frontend}:${TAG} to already exist"
fi

if [[ "$SKIP_LOAD" == false ]]; then
  log "loading images into kind cluster '${CLUSTER}'"
  for target in "${IMAGES[@]}"; do
    kind load docker-image "traders-${target}:${TAG}" --name "$CLUSTER"
  done
else
  log "skipping kind load (--skip-load)"
fi

kubectl --context "$KUBE_CONTEXT" get namespace "$NAMESPACE" >/dev/null 2>&1 \
  || { log "creating namespace ${NAMESPACE}"; kubectl --context "$KUBE_CONTEXT" create namespace "$NAMESPACE"; }

SECRET_NAME="${RELEASE}-secrets"

if [[ "$SKIP_SECRET" == false ]]; then
  if [[ -f "$ENV_FILE" ]]; then
    log "creating/updating Secret ${SECRET_NAME} from ${ENV_FILE}"
    # Only set keys that are actually non-empty in the env file. traders.py passes
    # e.g. os.getenv("OPENROUTER_API_KEY") straight into AsyncOpenAI(api_key=...),
    # and the openai SDK only falls back to OPENAI_API_KEY when that's None — an
    # empty-but-present env var (which is what an empty --from-literal produces)
    # breaks that fallback and crashes every client construction at import time.
    literal_args=()
    for key in "${SECRET_KEYS[@]}"; do
      value="$(grep -E "^${key}=" "$ENV_FILE" | tail -n1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'\$//")" || true
      if [[ -n "$value" ]]; then
        literal_args+=(--from-literal="${key}=${value}")
      fi
    done
    [[ ${#literal_args[@]} -gt 0 ]] || die "no non-empty keys found in ${ENV_FILE} among: ${SECRET_KEYS[*]}"
    kubectl --context "$KUBE_CONTEXT" -n "$NAMESPACE" create secret generic "$SECRET_NAME" \
      "${literal_args[@]}" --dry-run=client -o yaml | kubectl --context "$KUBE_CONTEXT" apply -f -
  else
    log "no env file at ${ENV_FILE}, skipping Secret creation — set secrets.existingSecret yourself or rerun with --env-file"
    SKIP_SECRET=true
  fi
else
  log "skipping Secret creation (--skip-secret)"
fi

helm_args=(
  upgrade --install "$RELEASE" "$CHART_DIR"
  --kube-context "$KUBE_CONTEXT"
  --namespace "$NAMESPACE"
  --set "image.repository=traders"
  --set "image.tag=${TAG}"
  --set "image.pullPolicy=Never"
)
if [[ "$SKIP_SECRET" == false ]]; then
  helm_args+=(--set "secrets.existingSecret=${SECRET_NAME}")
fi
if [[ "$DRY_RUN" == true ]]; then
  helm_args+=(--dry-run)
else
  helm_args+=(--wait --timeout 5m)
fi
if [[ ${#HELM_EXTRA_ARGS[@]} -gt 0 ]]; then
  helm_args+=("${HELM_EXTRA_ARGS[@]}")
fi

log "deploying release '${RELEASE}' to namespace '${NAMESPACE}' on context '${KUBE_CONTEXT}'"
helm "${helm_args[@]}"

if [[ "$DRY_RUN" == false ]]; then
  # With --pullPolicy Never, an unchanged tag string never triggers a rollout on
  # its own — but a rebuild against a dirty (uncommitted) tree reuses the same
  # tag while `kind load` overwrites its content on the node. Force a restart
  # so already-running pods actually pick up what was just built, every time.
  log "restarting workloads so they pick up the freshly loaded image content"
  for dep in "${RELEASE}-app" "${RELEASE}-frontend"; do
    if kubectl --context "$KUBE_CONTEXT" -n "$NAMESPACE" get deployment "$dep" >/dev/null 2>&1; then
      kubectl --context "$KUBE_CONTEXT" -n "$NAMESPACE" rollout restart "deployment/${dep}"
    fi
  done
  kubectl --context "$KUBE_CONTEXT" -n "$NAMESPACE" rollout status "deployment/${RELEASE}-app" --timeout=120s
  log "pods:"
  kubectl --context "$KUBE_CONTEXT" -n "$NAMESPACE" get pods
fi

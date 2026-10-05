#!/usr/bin/env bash
# One-command installer for the Bedrock Spend Controls demo.
#
# Orchestrates the documented steps (DEPLOYMENT.md, "Demo / personal
# account") with the shared preflight checks, then runs the smoke test.
# Every decision lives in Python (tools/preflight, cdk/stacks/configuration,
# tools/smoke_test.py); this script only sequences commands and reports.
#
# Runs on Amazon Linux 2023 (CloudShell) and macOS with the stock bash 3.2:
# no associative arrays, no ${var,,}, no mapfile.
#
#   ./install.sh --alert-email you@example.com
#   curl -fsSL https://raw.githubusercontent.com/aws-samples/sample-bedrock-spend-controls/main/install.sh \
#     | bash -s -- --alert-email you@example.com
#
# Exit status: 0 when every phase succeeded or was skipped; 1 otherwise (the
# failing phase is named on stderr and in the summary table).

set -euo pipefail

REPO_URL="https://github.com/aws-samples/sample-bedrock-spend-controls.git"
STACK_NAME="BedrockSpendControls"
OUTPUTS_FILE_NAME=".install-outputs.env"
NODE_MIN=20
PHASE_NAMES="preflight build bootstrap synth diff deploy outputs smoke done"

# Honoured environment: GIT_REF (branch or tag to clone, default main),
# INSTALL_DIR (clone target, default ~/sample-bedrock-spend-controls),
# ALERT_EMAIL / ADMIN_EMAIL (defaults for the flags), SMOKE_MODEL (model ID
# for the smoke test), AWS_PROFILE / AWS_REGION (defaults for --profile and
# --region).
GIT_REF="${GIT_REF:-main}"
INSTALL_DIR="${INSTALL_DIR:-$HOME/sample-bedrock-spend-controls}"

PROFILE="${AWS_PROFILE:-}"
REGION_OPT=""
CONFIG_OPT="demo"
ALERT_EMAIL_OPT=""
ADMIN_EMAIL_OPT=""
ACK_LOGGING=0
ASSUME_YES=0
SKIP_SMOKE=0
SKIP_PREFLIGHT=0
DESTROY=0
DRY_RUN=0

ORIGINAL_ARGS=("$@")
CURRENT_PHASE=""
PHASE_INDEX=0
PHASE_TOTAL=0
SUMMARY_WANTED=0
WORK_DIR=""

usage() {
  cat <<EOF
Usage: install.sh [--profile P] [--region R] [--config demo|path.json]
                  [--alert-email E] [--admin-email E] [--acknowledge-logging-overwrite]
                  [--yes] [--skip-smoke] [--skip-preflight] [--destroy] [--dry-run] [-h]

Deploys the Bedrock Spend Controls sample (demo profile by default) in nine
phases: preflight, build, bootstrap, synth, diff, deploy, outputs, smoke, done.

Options:
  --profile P                      AWS CLI profile (default: \$AWS_PROFILE)
  --region R                       target Region (default: \$AWS_REGION, then the profile's region)
  --config demo|path.json          deployment file: "demo" is cdk/config/demo.json (default)
  --alert-email E                  SNS alert address; required for demo (or export ALERT_EMAIL)
  --admin-email E                  console administrator address; demo default: the alert email
  --acknowledge-logging-overwrite  accept that the demo overwrites the Region's Bedrock
                                   invocation logging configuration (preflight fails without it
                                   when logging is already configured)
  --yes                            no questions: continue on preflight warnings, skip the
                                   diff confirmation, confirm --destroy
  --skip-smoke                     do not run tools/smoke_test.py after the deploy
  --skip-preflight                 do not run tools/preflight (bootstrap is then always attempted)
  --destroy                        run cdk destroy for the stack and list what is retained
  --dry-run                        print the commands (prefixed "+") without running them
  -h, --help                       show this help

Environment: GIT_REF (default main) and INSTALL_DIR (default ~/sample-bedrock-spend-controls)
control the clone made when the script does not run from a checkout; SMOKE_MODEL overrides
the smoke test's model ID. Outputs are written to $OUTPUTS_FILE_NAME (mode 600) in the
repository root.
EOF
}

say() { printf '%s\n' "$*"; }
warn() { printf 'install.sh: %s\n' "$*" >&2; }
die() { warn "$@"; exit 1; }

# Render arguments the way a shell would accept them, for the "+ cmd" lines
# (printf %q: plain words stay as they are, spaces and quotes get escaped).
shell_quote() {
  local arg out="" piece
  for arg in "$@"; do
    piece="$(printf '%q' "$arg")"
    if [ -n "$out" ]; then out="$out $piece"; else out="$piece"; fi
  done
  printf '%s' "$out"
}

show() { printf '+ %s\n' "$(shell_quote "$@")"; }

# run CMD...: print, then execute unless --dry-run.
run() {
  show "$@"
  if [ "$DRY_RUN" = 1 ]; then return 0; fi
  "$@"
}

# run_in DIR CMD...: same, executed inside DIR.
run_in() {
  local dir="$1"
  shift
  printf '+ (cd %s && %s)\n' "$(shell_quote "$dir")" "$(shell_quote "$@")"
  if [ "$DRY_RUN" = 1 ]; then return 0; fi
  (cd "$dir" && "$@")
}

# capture VAR CMD...: like run, storing stdout in VAR (empty in --dry-run).
capture() {
  local __var="$1" __out=""
  shift
  show "$@"
  if [ "$DRY_RUN" != 1 ]; then __out="$("$@")"; fi
  printf -v "$__var" '%s' "$__out"
}

have_tty() { { : </dev/tty; } 2>/dev/null; }

# confirm QUESTION: ask on the terminal (never stdin: it may be the script
# itself under "curl | bash"); --yes answers for the operator.
confirm() {
  local answer=""
  if [ "$ASSUME_YES" = 1 ]; then
    say "$1 [--yes]"
    return 0
  fi
  if [ "$DRY_RUN" = 1 ]; then
    say "$1 [dry run: would ask]"
    return 0
  fi
  have_tty || die "cannot ask \"$1\" without a terminal; re-run with --yes"
  printf '%s [y/N] ' "$1"
  read -r answer </dev/tty
  case "$answer" in
    y | Y | yes | YES | Yes) return 0 ;;
    *) die "aborted" ;;
  esac
}

# ask VAR PROMPT: read a value from the terminal into VAR.
ask() {
  local __var="$1" __value=""
  have_tty || return 1
  printf '%s: ' "$2"
  read -r __value </dev/tty
  printf -v "$__var" '%s' "$__value"
}

valid_email() {
  case "$1" in
    *[[:space:]]* | *@*@* | @* | *@ | "" ) return 1 ;;
    *@*.* ) return 0 ;;
    * ) return 1 ;;
  esac
}

abs_path() { (cd "$(dirname "$1")" && printf '%s/%s\n' "$(pwd -P)" "$(basename "$1")"); }

# --- phase bookkeeping (plain variables: bash 3.2 has no associative arrays)

set_status() { printf -v "STATUS_$1" '%s' "$2"; }
get_status() {
  local name="STATUS_$1"
  printf '%s' "${!name:--}"
}

begin_phase() {
  CURRENT_PHASE="$1"
  PHASE_INDEX=$((PHASE_INDEX + 1))
  printf '\n==> [%d/%d] %s\n' "$PHASE_INDEX" "$PHASE_TOTAL" "$1"
}

end_phase() {
  set_status "$CURRENT_PHASE" "${1:-OK}"
  CURRENT_PHASE=""
}

skip_phase() {
  begin_phase "$1"
  say "skipped: $2"
  end_phase SKIPPED
}

print_summary() {
  local name
  say ""
  say "Summary"
  for name in $PHASE_NAMES; do
    printf '  %-10s %s\n' "$name" "$(get_status "$name")"
  done
}

on_exit() {
  local status=$?
  trap - EXIT
  if [ "$status" -ne 0 ] && [ -n "$CURRENT_PHASE" ]; then
    set_status "$CURRENT_PHASE" FAILED
  fi
  if [ "$SUMMARY_WANTED" = 1 ]; then
    print_summary
  fi
  if [ "$status" -ne 0 ] && [ -n "$CURRENT_PHASE" ]; then
    warn "phase $CURRENT_PHASE failed (exit status $status)"
  fi
  if [ -n "$WORK_DIR" ]; then rm -rf "$WORK_DIR"; fi
  exit "$status"
}

# --- arguments ----------------------------------------------------------------

need_value() {
  if [ "$#" -lt 2 ] || [ -z "$2" ]; then die "$1 needs a value (see --help)"; fi
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --profile) need_value "$@"; PROFILE="$2"; shift 2 ;;
    --profile=*) PROFILE="${1#*=}"; shift ;;
    --region) need_value "$@"; REGION_OPT="$2"; shift 2 ;;
    --region=*) REGION_OPT="${1#*=}"; shift ;;
    --config) need_value "$@"; CONFIG_OPT="$2"; shift 2 ;;
    --config=*) CONFIG_OPT="${1#*=}"; shift ;;
    --alert-email) need_value "$@"; ALERT_EMAIL_OPT="$2"; shift 2 ;;
    --alert-email=*) ALERT_EMAIL_OPT="${1#*=}"; shift ;;
    --admin-email) need_value "$@"; ADMIN_EMAIL_OPT="$2"; shift 2 ;;
    --admin-email=*) ADMIN_EMAIL_OPT="${1#*=}"; shift ;;
    --acknowledge-logging-overwrite) ACK_LOGGING=1; shift ;;
    --yes | -y) ASSUME_YES=1; shift ;;
    --skip-smoke) SKIP_SMOKE=1; shift ;;
    --skip-preflight) SKIP_PREFLIGHT=1; shift ;;
    --destroy) DESTROY=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h | --help) usage; exit 0 ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
done

# --- repository root (clone and re-exec under "curl | bash") ------------------

ROOT=""
SCRIPT_PATH="${BASH_SOURCE[0]:-}"
if [ -n "$SCRIPT_PATH" ] && [ -f "$SCRIPT_PATH" ]; then
  ROOT="$(cd "$(dirname "$SCRIPT_PATH")" && pwd -P)"
  if [ ! -f "$ROOT/cdk/app.py" ] || [ ! -d "$ROOT/tools/preflight" ]; then
    ROOT=""
  fi
fi
if [ -z "$ROOT" ]; then
  if [ "${BSC_INSTALL_REEXEC:-0}" = 1 ]; then
    die "$INSTALL_DIR does not look like a checkout of the repository"
  fi
  if [ -f "$INSTALL_DIR/install.sh" ]; then
    say "Using the existing checkout at $INSTALL_DIR (GIT_REF is ignored)"
  else
    command -v git >/dev/null 2>&1 || die "git is required to clone $REPO_URL"
    say "Cloning $REPO_URL ($GIT_REF) into $INSTALL_DIR"
    run git clone --branch "$GIT_REF" --depth 1 "$REPO_URL" "$INSTALL_DIR"
    if [ "$DRY_RUN" = 1 ]; then
      die "dry run outside a checkout: clone first, then run $INSTALL_DIR/install.sh --dry-run"
    fi
  fi
  BSC_INSTALL_REEXEC=1 exec bash "$INSTALL_DIR/install.sh" ${ORIGINAL_ARGS[@]+"${ORIGINAL_ARGS[@]}"}
fi

# --- toolchain ----------------------------------------------------------------

find_python() {
  local candidate
  for candidate in python3 python3.12 python3.13 python3.14 python; do
    if command -v "$candidate" >/dev/null 2>&1 \
      && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}

node_version_ok() {
  local version major
  command -v node >/dev/null 2>&1 || return 1
  version="$(node --version 2>/dev/null)" || return 1
  major="${version#v}"
  major="${major%%.*}"
  case "$major" in
    '' | *[!0-9]*) return 1 ;;
  esac
  [ "$major" -ge "$NODE_MIN" ]
}

if ! node_version_ok; then
  cat >&2 <<EOF
install.sh: Node.js $NODE_MIN or newer is required (found: $(node --version 2>/dev/null || echo none)).
This script does not install software. On CloudShell / Amazon Linux 2023:
  curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash
  . ~/.nvm/nvm.sh && nvm install 22 && nvm use 22
On macOS: brew install node@22 (or use nvm as above). Then re-run install.sh.
EOF
  exit 1
fi
command -v npm >/dev/null 2>&1 || die "npm is required (it ships with Node.js)"
command -v aws >/dev/null 2>&1 || die "the AWS CLI v2 is required: https://aws.amazon.com/cli/"
PY="$(find_python)" || {
  cat >&2 <<EOF
install.sh: Python 3.12 or newer is required (python3 is $(python3 --version 2>/dev/null || echo missing)).
This script does not install software. On CloudShell / Amazon Linux 2023:
  sudo dnf install -y python3.12
On macOS: brew install python@3.12. Then re-run install.sh.
EOF
  exit 1
}

# --- configuration and target ---------------------------------------------------

CONFIG_IS_DEMO=0
case "$CONFIG_OPT" in
  demo)
    CONFIG_PATH="$ROOT/cdk/config/demo.json"
    CONFIG_IS_DEMO=1
    ;;
  *)
    [ -f "$CONFIG_OPT" ] || die "config file not found: $CONFIG_OPT"
    CONFIG_PATH="$(abs_path "$CONFIG_OPT")"
    ;;
esac

ALERT_EMAIL="${ALERT_EMAIL_OPT:-${ALERT_EMAIL:-}}"
ADMIN_EMAIL="${ADMIN_EMAIL_OPT:-${ADMIN_EMAIL:-}}"
if [ "$DESTROY" = 0 ] && [ "$CONFIG_IS_DEMO" = 1 ] && [ -z "$ALERT_EMAIL" ]; then
  if [ "$ASSUME_YES" = 0 ] && [ "$DRY_RUN" = 0 ] && have_tty; then
    ask ALERT_EMAIL "Email address for quota alerts (SNS subscription)" || true
  fi
  [ -n "$ALERT_EMAIL" ] || die "--alert-email is required for the demo configuration (or export ALERT_EMAIL)"
fi
if [ "$DESTROY" = 0 ] && [ "$CONFIG_IS_DEMO" = 1 ] && [ -z "$ADMIN_EMAIL" ]; then
  ADMIN_EMAIL="$ALERT_EMAIL"
fi
if [ -n "$ALERT_EMAIL" ] && ! valid_email "$ALERT_EMAIL"; then
  die "--alert-email must be one address (name@domain): $ALERT_EMAIL"
fi
if [ -n "$ADMIN_EMAIL" ] && ! valid_email "$ADMIN_EMAIL"; then
  die "--admin-email must be one address (name@domain): $ADMIN_EMAIL"
fi

REGION="${REGION_OPT:-${AWS_REGION:-${AWS_DEFAULT_REGION:-}}}"
if [ -z "$REGION" ] && [ "$DRY_RUN" = 0 ]; then
  if [ -n "$PROFILE" ]; then
    REGION="$(aws configure get region --profile "$PROFILE" 2>/dev/null || true)"
  else
    REGION="$(aws configure get region 2>/dev/null || true)"
  fi
fi
[ -n "$REGION" ] || die "no AWS Region: pass --region or export AWS_REGION"
case "$REGION" in
  *[!a-z0-9-]* | [!a-z]*) die "not an AWS Region name: $REGION" ;;
esac

export AWS_REGION="$REGION"
export AWS_DEFAULT_REGION="$REGION"
export CDK_DEFAULT_REGION="$REGION"
export JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION=1
if [ -n "$PROFILE" ]; then
  export AWS_PROFILE="$PROFILE"
fi

PY_EX="$ROOT/.venv-examples/bin/python"
PY_CDK="$ROOT/cdk/.venv/bin/python"
OUTPUTS_FILE="$ROOT/$OUTPUTS_FILE_NAME"
WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/bsc-install.XXXXXX")"
trap on_exit EXIT

CDK_CONTEXT=(-c "deployment_config=$CONFIG_PATH")
if [ -n "$ALERT_EMAIL" ]; then CDK_CONTEXT+=(-c "alert_email=$ALERT_EMAIL"); fi
if [ -n "$ADMIN_EMAIL" ]; then CDK_CONTEXT+=(-c "admin_email=$ADMIN_EMAIL"); fi

say "Bedrock Spend Controls installer"
say "  repository:  $ROOT"
say "  config:      $CONFIG_PATH"
say "  region:      $REGION"
say "  profile:     ${PROFILE:-(default credentials)}"
say "  stack:       $STACK_NAME"
if [ "$DESTROY" = 0 ]; then
  say "  alert email: ${ALERT_EMAIL:-(from the config file)}"
  say "  admin email: ${ADMIN_EMAIL:-(none: no console administrator is created)}"
fi
if [ "$DRY_RUN" = 1 ]; then
  say "  mode:        dry run (commands are printed, nothing is executed)"
fi

# Credentials: the account is needed for CDK_DEFAULT_ACCOUNT; in a dry run
# nothing contacts AWS.
ACCOUNT=""
if [ "$DRY_RUN" = 0 ]; then
  if ! ACCOUNT="$(aws sts get-caller-identity --query Account --output text 2>"$WORK_DIR/sts.err")"; then
    cat "$WORK_DIR/sts.err" >&2
    die "AWS credentials are not usable${PROFILE:+ (profile $PROFILE)}; run: aws sso login${PROFILE:+ --profile $PROFILE}"
  fi
  export CDK_DEFAULT_ACCOUNT="$ACCOUNT"
  say "  account:     $ACCOUNT"
else
  ACCOUNT="111122223333"  # placeholder: a dry run never contacts AWS
fi

# --- helpers shared by the phases -----------------------------------------------

ensure_examples_venv() {
  # boto3 + httpx for tools/smoke_test.py (pinned in examples/requirements.txt),
  # the same virtualenv DEPLOYMENT.md step 6 creates.
  if [ ! -x "$PY_EX" ]; then
    run "$PY" -m venv "$ROOT/.venv-examples"
  fi
  run "$PY_EX" -m pip install --quiet --disable-pip-version-check -r "$ROOT/examples/requirements.txt"
}

CDK_VENV_READY=0
ensure_cdk_venv() {
  # The CDK app's virtualenv (cdk.json runs .venv/bin/python app.py) plus the
  # pinned boto3/httpx, so tools/preflight can import
  # cdk/stacks/configuration.py and validate exactly as cdk synth will.
  if [ "$CDK_VENV_READY" = 1 ]; then return 0; fi
  if [ ! -x "$PY_CDK" ]; then
    run "$PY" -m venv "$ROOT/cdk/.venv"
  fi
  run "$PY_CDK" -m pip install --quiet --disable-pip-version-check --upgrade pip
  run "$ROOT/cdk/.venv/bin/pip" install --quiet --disable-pip-version-check \
    -r "$ROOT/cdk/requirements.txt" -r "$ROOT/examples/requirements.txt"
  CDK_VENV_READY=1
}

ensure_cdk_toolchain() {
  ensure_cdk_venv
  if [ ! -x "$ROOT/cdk/node_modules/.bin/cdk" ]; then
    run_in "$ROOT/cdk" npm ci
  fi
}

config_flag() {
  # config_flag KEY: "true" or "false" as written in the deployment file.
  "$PY" -c 'import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8")).get(sys.argv[2], False)
print("true" if value is True or str(value).strip().lower() == "true" else "false")' "$CONFIG_PATH" "$1"
}

stack_exists() {
  aws cloudformation describe-stacks --stack-name "$STACK_NAME" >/dev/null 2>&1
}

stack_status() {
  aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
    --query 'Stacks[0].StackStatus' --output text 2>/dev/null || true
}

# A stack whose first create failed sits in ROLLBACK_COMPLETE and cannot be
# updated: CloudFormation requires deleting it before creating it again.
recover_failed_stack() {
  local status
  status="$(stack_status)"
  case "$status" in
    ROLLBACK_COMPLETE|ROLLBACK_FAILED|CREATE_FAILED|DELETE_FAILED)
      say "stack $STACK_NAME is in $status: a previous create failed and CloudFormation cannot update it"
      say "(cdk deploy printed the failing resource; the log groups it kept may hold the reason)"
      confirm "Delete the failed stack and create it again?"
      run aws cloudformation delete-stack --stack-name "$STACK_NAME"
      run aws cloudformation wait stack-delete-complete --stack-name "$STACK_NAME"
      ;;
  esac
}

# The stack RETAINs its invocation log group on delete and on rollback, so
# a second create in the same Region fails with "already exists" unless
# that orphan goes first. Only for a fresh create with stack-managed logging.
remove_orphaned_log_group() {
  local group="/bedrock/spend-controls/model-invocations" found=""
  if stack_exists; then return 0; fi
  if [ "$(config_flag manage_invocation_logging)" != true ]; then return 0; fi
  found="$(aws logs describe-log-groups --log-group-name-prefix "$group" \
    --query "logGroups[?logGroupName=='$group'].logGroupName" --output text 2>/dev/null || true)"
  if [ "$found" != "$group" ]; then return 0; fi
  say "log group $group exists without the stack (kept by an earlier install's rollback or destroy);"
  say "CloudFormation cannot create it again while it is there"
  confirm "Delete $group (its old invocation logs are lost) so the new stack can own it?"
  run aws logs delete-log-group --log-group-name "$group"
}

# --- destroy ----------------------------------------------------------------------

if [ "$DESTROY" = 1 ]; then
  PHASE_TOTAL=1
  SUMMARY_WANTED=0
  begin_phase destroy
  ensure_cdk_toolchain
  confirm "Destroy stack $STACK_NAME in $REGION${PROFILE:+ (profile $PROFILE)}?"
  run_in "$ROOT/cdk" npx cdk destroy --force "${CDK_CONTEXT[@]}"
  if [ -f "$OUTPUTS_FILE" ]; then
    run rm -f "$OUTPUTS_FILE"
  fi
  end_phase OK
  cat <<EOF

Retained on purpose (the stack cannot restore the account-wide logging setting):
  - the Bedrock model-invocation logging configuration of $REGION
  - the log group /bedrock/spend-controls/model-invocations
  - the Bedrock logging role (name starts with $STACK_NAME-BedrockLoggingRole)
  - with retain_tables_on_delete=true, the DynamoDB tables (deletion protection on)
To remove them once nothing else depends on them:
  aws bedrock delete-model-invocation-logging-configuration
  aws logs delete-log-group --log-group-name /bedrock/spend-controls/model-invocations
  aws iam list-roles --query "Roles[?starts_with(RoleName, '$STACK_NAME-BedrockLoggingRole')].RoleName" --output text
  aws iam delete-role-policy --role-name <role> --policy-name WriteInvocationLogs && aws iam delete-role --role-name <role>
See DEPLOYMENT.md, "Clean up".
EOF
  exit 0
fi

# --- install ---------------------------------------------------------------------

PHASE_TOTAL=9
SUMMARY_WANTED=1

# 1. preflight
BOOTSTRAP_STATUS="unknown"
if [ "$SKIP_PREFLIGHT" = 1 ]; then
  skip_phase preflight "--skip-preflight"
else
  begin_phase preflight
  ensure_cdk_venv
  PREFLIGHT_ARGS=(-m tools.preflight --config "$CONFIG_PATH" --region "$REGION" --json)
  if [ -n "$PROFILE" ]; then PREFLIGHT_ARGS+=(--profile "$PROFILE"); fi
  if [ -n "$ALERT_EMAIL" ]; then PREFLIGHT_ARGS+=(--set "alert_email=$ALERT_EMAIL"); fi
  if [ -n "$ADMIN_EMAIL" ]; then PREFLIGHT_ARGS+=(--set "admin_email=$ADMIN_EMAIL"); fi
  if [ "$ACK_LOGGING" = 1 ]; then PREFLIGHT_ARGS+=(--acknowledge-logging-overwrite); fi
  show "$PY_CDK" "${PREFLIGHT_ARGS[@]}"
  if [ "$DRY_RUN" = 1 ]; then
    say "(dry run: the preflight was not executed)"
  else
    set +e
    (cd "$ROOT" && "$PY_CDK" "${PREFLIGHT_ARGS[@]}" >"$WORK_DIR/preflight.json")
    PREFLIGHT_RC=$?
    set -e
    if [ "$PREFLIGHT_RC" -eq 2 ] || [ ! -s "$WORK_DIR/preflight.json" ]; then
      die "preflight could not run (usage or configuration error above)"
    fi
    # Render the table with the installer's reading of it (a missing
    # bootstrap or console build is handled by the next phases).
    (cd "$ROOT" && "$PY_CDK" -m tools.preflight.verdict --render "$WORK_DIR/preflight.json")
    # A missing bootstrap or an unbuilt console are not failures here: the
    # next phases bootstrap and build (tools/preflight/verdict.py).
    read -r PREFLIGHT_VERDICT PREFLIGHT_WARNINGS BOOTSTRAP_STATUS <<EOF
$(cd "$ROOT" && "$PY_CDK" -m tools.preflight.verdict "$WORK_DIR/preflight.json")
EOF
    if [ "$PREFLIGHT_VERDICT" != ok ]; then
      die "preflight failed; fix the FAIL lines above (or re-run with --skip-preflight at your own risk)"
    fi
    if [ "$PREFLIGHT_WARNINGS" = warn ]; then
      confirm "Preflight reported warnings (see above). Continue?"
    fi
  fi
  end_phase OK
fi

# 2. build
begin_phase build
ADMIN_UI="$(config_flag admin_ui)"
if [ "$ADMIN_UI" = true ]; then
  run_in "$ROOT/admin-ui" npm ci
  run_in "$ROOT/admin-ui" npm run build
else
  say "admin_ui is false in the config: the console is not built"
fi
ensure_cdk_venv
run_in "$ROOT/cdk" npm ci
end_phase OK

# 3. bootstrap
if [ "$BOOTSTRAP_STATUS" = pass ]; then
  skip_phase bootstrap "CDKToolkit is present and current (preflight)"
else
  begin_phase bootstrap
  if [ "$BOOTSTRAP_STATUS" = unknown ]; then
    say "preflight did not run: cdk bootstrap is idempotent, so it runs anyway"
  else
    say "preflight bootstrap check: $BOOTSTRAP_STATUS"
  fi
  # The CLI synthesizes the app even for bootstrap, so the deployment
  # context must come along; the explicit environment avoids a lookup.
  run_in "$ROOT/cdk" npx cdk bootstrap "aws://$ACCOUNT/$REGION" "${CDK_CONTEXT[@]}"
  end_phase OK
fi

# 4. synth
begin_phase synth
run_in "$ROOT/cdk" npx cdk synth "${CDK_CONTEXT[@]}" --quiet
say "template written to cdk/cdk.out"
end_phase OK

# 5. diff
begin_phase diff
if [ "$DRY_RUN" = 1 ]; then
  show aws cloudformation describe-stacks --stack-name "$STACK_NAME"
  say "(dry run) when the stack already exists:"
  run_in "$ROOT/cdk" npx cdk diff "${CDK_CONTEXT[@]}"
  end_phase OK
elif stack_exists; then
  say "stack $STACK_NAME exists: showing the changes this deploy would make"
  run_in "$ROOT/cdk" npx cdk diff "${CDK_CONTEXT[@]}"
  confirm "Deploy these changes to $STACK_NAME?"
  end_phase OK
else
  say "stack $STACK_NAME does not exist yet: nothing to diff"
  end_phase SKIPPED
fi

# 6. deploy
begin_phase deploy
if [ "$DRY_RUN" = 1 ]; then
  say "(dry run) would delete a stack left in ROLLBACK_COMPLETE by a failed create, and an orphaned"
  say "(dry run) /bedrock/spend-controls/model-invocations log group, after asking"
else
  recover_failed_stack
  remove_orphaned_log_group
fi
run_in "$ROOT/cdk" npx cdk deploy --require-approval never "${CDK_CONTEXT[@]}"
end_phase OK

# 7. outputs
begin_phase outputs
ADMIN_UI_URL=""
BROKER_API_URL=""
capture STACK_JSON aws cloudformation describe-stacks --stack-name "$STACK_NAME" --output json
if [ "$DRY_RUN" = 1 ]; then
  say "(dry run) would write $OUTPUTS_FILE (mode 600)"
else
  printf '%s' "$STACK_JSON" >"$WORK_DIR/stack.json"
  (umask 077 && "$PY" - "$WORK_DIR/stack.json" "$OUTPUTS_FILE" "$STACK_NAME" "$REGION" <<'PY'
import json, shlex, sys
stack_json, target, stack_name, region = sys.argv[1:5]
with open(stack_json, encoding="utf-8") as handle:
    stacks = json.load(handle)["Stacks"]
outputs = {o["OutputKey"]: o["OutputValue"] for o in stacks[0].get("Outputs", [])}
values = {
    "BROKER_API_URL": outputs.get("BrokerApiUrl", "").rstrip("/"),
    "ADMIN_UI_URL": outputs.get("AdminUiUrl", ""),
    "USER_POOL_ID": outputs.get("DemoUserPoolId", ""),
    "CLIENT_ID": outputs.get("DemoUserPoolClientId", ""),
    "STACK_NAME": stack_name,
    "AWS_REGION": region,
}
with open(target, "w", encoding="utf-8") as handle:
    handle.write("# Written by install.sh; stack outputs (no secrets). Source it with: . %s\n" % shlex.quote(target))
    for key, value in values.items():
        handle.write("%s=%s\n" % (key, shlex.quote(value)))
PY
  )
  chmod 600 "$OUTPUTS_FILE"
  # shellcheck source=/dev/null
  . "$OUTPUTS_FILE"
  say "outputs written to $OUTPUTS_FILE"
  say "  BROKER_API_URL=$BROKER_API_URL"
  if [ -n "$ADMIN_UI_URL" ]; then say "  ADMIN_UI_URL=$ADMIN_UI_URL"; fi
fi
end_phase OK

# 8. smoke
if [ "$SKIP_SMOKE" = 1 ]; then
  skip_phase smoke "--skip-smoke"
else
  begin_phase smoke
  ensure_examples_venv
  SMOKE_ARGS=("$ROOT/tools/smoke_test.py" --region "$REGION" --stack "$STACK_NAME")
  if [ -n "$PROFILE" ]; then SMOKE_ARGS+=(--profile "$PROFILE"); fi
  if [ -n "${SMOKE_MODEL:-}" ]; then SMOKE_ARGS+=(--model "$SMOKE_MODEL"); fi
  run "$PY_EX" "${SMOKE_ARGS[@]}"
  end_phase OK
fi

# 9. done
begin_phase done
say "Deployed $STACK_NAME to $REGION."
if [ -n "$ADMIN_UI_URL" ]; then
  say "  Console:       $ADMIN_UI_URL"
elif [ "$DRY_RUN" = 1 ]; then
  say "  Console:       AdminUiUrl stack output"
fi
if [ -n "$BROKER_API_URL" ]; then
  say "  Broker API:    $BROKER_API_URL"
fi
if [ -n "$ADMIN_EMAIL" ]; then
  say "  Administrator: user quota-admin; check $ADMIN_EMAIL for the temporary password"
  say "                 (sent by Cognito from no-reply@verificationemail.com; the first sign-in replaces it)"
fi
if [ -n "$ALERT_EMAIL" ]; then
  say "  Alerts:        confirm the SNS subscription that AWS Notifications sent to $ALERT_EMAIL"
fi
say "  Outputs:       $OUTPUTS_FILE"
say "  Next steps:    DEPLOYMENT.md (smoke tests by hand), docs/operations.md, docs/integration.md"
say "  Remove:        $ROOT/install.sh --destroy${PROFILE:+ --profile $PROFILE} --region $REGION"
end_phase OK

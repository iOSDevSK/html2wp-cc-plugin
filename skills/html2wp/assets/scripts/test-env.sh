#!/usr/bin/env bash
# Isolated, per-run WordPress test environment for html2wp-sub, stage 5.
#
#   test-env.sh up <slug>            create/reuse a throwaway WP for <slug>
#   test-env.sh check <slug> [theme] assert it is still that same environment
#   test-env.sh down <slug>          remove containers, volumes, network
#
# Run from the conversion workspace — `up` writes a state file there
# (`.test-env-<slug>.json`) that `check` and `down` read back, and that later
# stages should read too instead of re-deriving the URL/container/port.
#
# This exists because of two real failures on the old shared clara-test-wp
# container: two conversions racing `wp core install` against the SAME
# container name corrupted both runs, and a container's active theme
# changing mid-run (someone else switched it) silently broke whichever gate
# ran next with no clue why. Every run here gets its own docker compose
# project (`h2wp-<slug>-<runid>`), so its containers, network and volumes
# cannot be the shared ones and cannot be touched by another run of this
# script — `up`/`check`/`down` all refuse to operate on a project name that
# is not prefixed `h2wp-` or that contains "clara-test".
#
# `up` is idempotent: re-running it for a slug that already has a state file
# reuses that project (and hence its port, container names, and whatever
# `docker compose up -d` already created) instead of generating a new run —
# so a crash partway through `up` is recovered by just running `up` again.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="$SCRIPT_DIR/test-env-compose.yml"

sanitize_slug() {
  local s
  s="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//')"
  if [[ -z "$s" ]]; then
    echo "test-env.sh: empty slug after sanitizing '$1'" >&2
    exit 2
  fi
  printf '%s' "$s"
}

state_path() {
  printf '%s/.test-env-%s.json' "$PWD" "$1"
}

gen_run_id() {
  # 6 lowercase hex chars — short, and printf %x is already lowercase.
  printf '%06x' "$(( (RANDOM * 32768 + RANDOM) % 16777216 ))"
}

# Defense in depth: even though $PROJECT is always built from a fixed literal
# prefix here, refuse to run docker compose against anything that does not
# carry it, or that mentions clara-test — belt and braces against a future
# edit that constructs PROJECT differently.
require_safe_project() {
  local p="$1"
  case "$p" in
    h2wp-*) ;;
    *) echo "test-env.sh: REFUSING — project '$p' lacks the required h2wp- prefix" >&2; exit 1 ;;
  esac
  case "$p" in
    *clara-test*) echo "test-env.sh: REFUSING — project '$p' would touch the shared clara-test-* environment" >&2; exit 1 ;;
  esac
}

compose() {
  docker compose -p "$PROJECT" -f "$COMPOSE_FILE" "$@"
}

# Must match test-env-compose.yml's MARIADB_ROOT_PASSWORD.
DB_ROOT_PW="rootpw"

# The official wordpress:latest image ships neither wp-cli nor a mysql
# client (verified empirically: `which wp mysql mysqlcheck` all come back
# empty in a fresh container) — wp-cli only exists on the shared
# clara-test-wp container because someone installed it there once and that
# container has never been recreated since. Every fresh container needs it
# installed for real; php and curl ARE present in the base image, so this is
# the same phar-download reset-test-wp.sh's own comment implies is missing.
ensure_wp_cli() {
  local wp_ct="$1"
  if docker exec "$wp_ct" sh -c 'command -v wp' >/dev/null 2>&1; then
    return 0
  fi
  # Pinned to a RELEASE, with its published sha512 checked.
  #
  # This used to pull `builds/gh-pages/phar/wp-cli.phar`, which is the moving
  # nightly-ish build, with no version and no checksum — installed INTO the
  # container this file pins by Docker digest specifically so that the
  # environment under a cohort's gate results holds still. The argument the
  # compose file makes at length applied to everything except the one binary
  # that drives the install.
  #
  # Raise WP_CLI_VERSION deliberately, and update the checksum with it (the
  # project publishes wp-cli-<v>.phar.sha512 beside the phar).
  local wp_cli_version="${WP_CLI_VERSION:-2.12.0}"
  # From the project's own wp-cli-2.12.0.phar.sha512, checked against a real
  # download of the phar before it was written here.
  local wp_cli_sha512="${WP_CLI_SHA512:-be928f6b8ca1e8dfb9d2f4b75a13aa4aee0896f8a9a0a1c45cd5d2c98605e6172e6d014dda2e27f88c98befc16c040cbb2bd1bfa121510ea5cdf5f6a30fe8832}"
  echo "==> installing wp-cli $wp_cli_version into $wp_ct"
  docker exec -e V="$wp_cli_version" -e SUM="$wp_cli_sha512" "$wp_ct" sh -c '
    set -e
    url="https://github.com/wp-cli/wp-cli/releases/download/v$V/wp-cli-$V.phar"
    curl -fsSL --max-time 120 -o /tmp/wp.phar "$url"
    # The published checksum is the point of pinning a version; without it the
    # pin only documents an intention.
    if command -v sha512sum >/dev/null 2>&1; then
      echo "$SUM  /tmp/wp.phar" | sha512sum -c - >/dev/null || {
        echo "wp-cli checksum mismatch — refusing to install it" >&2
        echo "  expected $SUM" >&2
        echo "  got      $(sha512sum /tmp/wp.phar | cut -d" " -f1)" >&2
        exit 1; }
    else
      echo "     (no sha512sum in the container; checksum not verified)" >&2
    fi
    mv /tmp/wp.phar /usr/local/bin/wp
    chmod +x /usr/local/bin/wp
  '
}

# Because the WP container has no mysql client, `wp --allow-root db check`
# is not a usable readiness probe (same reason reset-test-wp.sh drops the
# database from the DB container rather than via wp-cli). Poll the DB
# container with ITS OWN client instead.
wait_for_db() {
  local db_ct="$1" i
  for i in $(seq 1 60); do
    if docker exec "$db_ct" sh -c "mariadb -uroot -p'$DB_ROOT_PW' -e 'SELECT 1;'" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  echo "test-env.sh: database in $db_ct never became reachable" >&2
  return 1
}

resolve_container_name() {
  # Robust against compose's naming scheme changing: ask compose for the
  # container id of the service, then ask docker for its real name, rather
  # than assuming "${PROJECT}-wp-1".
  local service="$1" id name
  id="$(compose ps -q "$service")"
  if [[ -z "$id" ]]; then
    echo "test-env.sh: no container for service '$service' in project '$PROJECT'" >&2
    exit 1
  fi
  name="$(docker inspect --format '{{.Name}}' "$id")"
  printf '%s' "${name#/}"
}

up_cmd() {
  local slug="${1:?usage: test-env.sh up <slug>}"
  local SLUG state
  SLUG="$(sanitize_slug "$slug")"
  state="$(state_path "$SLUG")"

  if [[ -f "$state" ]]; then
    PROJECT="$(jq -r '.project' "$state")"
    echo "==> reusing existing run for '$SLUG': project=$PROJECT"
  else
    PROJECT="h2wp-${SLUG}-$(gen_run_id)"
    echo "==> new run for '$SLUG': project=$PROJECT"
  fi
  require_safe_project "$PROJECT"

  echo "==> docker compose up -d"
  compose up -d

  local WP_CT DB_CT NETWORK PORT_RAW PORT URL
  WP_CT="$(resolve_container_name wp)"
  DB_CT="$(resolve_container_name db)"
  NETWORK="${PROJECT}_default"
  require_safe_project "$WP_CT"   # paranoia: never operate on a resolved name outside the h2wp- family either

  PORT_RAW="$(compose port wp 80)"
  PORT="${PORT_RAW##*:}"
  if [[ -z "$PORT" || "$PORT" == "$PORT_RAW" ]]; then
    echo "test-env.sh: could not read the published port for $WP_CT (got '$PORT_RAW')" >&2
    exit 1
  fi
  URL="http://localhost:${PORT}"

  ensure_wp_cli "$WP_CT"

  # The official wordpress:latest image defaults to upload_max_filesize=2M /
  # post_max_size=8M — plenty for wp-cli but not for uploading a theme ZIP
  # (often 30MB+) through Appearance > Themes > Upload, which is a silent
  # failure: PHP truncates the upload before WordPress ever sees it, and the
  # admin screen after the redirect just looks unexpectedly empty. Verified
  # against the shared clara-test-wp container, which only works today
  # because someone raised these by hand once; do it here so it does not
  # have to be rediscovered per run.
  local upload_max
  upload_max="$(docker exec "$WP_CT" php -r 'echo ini_get("upload_max_filesize");' 2>/dev/null || true)"
  if [[ "$upload_max" != "64M" ]]; then
    docker exec "$WP_CT" sh -c 'cat > /usr/local/etc/php/conf.d/uploads.ini <<EOF
upload_max_filesize = 64M
post_max_size = 64M
memory_limit = 512M
max_execution_time = 300
EOF'
    # Do not signal Apache while the image entrypoint is still starting it. On
    # a fresh container the PID file may not exist yet; apache2ctl graceful then
    # starts a second daemon, loses the port-80 race and exits the container's
    # foreground process. A container restart is deterministic and also proves
    # the new php.ini survives the exact lifecycle it will run under.
    docker restart "$WP_CT" >/dev/null
    # Docker Desktop can allocate a new ephemeral host port on restart. Refresh
    # the address after the lifecycle change or the state file can point at the
    # dead pre-restart port even though WordPress itself is healthy.
    PORT_RAW="$(compose port wp 80)"
    PORT="${PORT_RAW##*:}"
    if [[ -z "$PORT" || "$PORT" == "$PORT_RAW" ]]; then
      echo "test-env.sh: could not refresh the published port for $WP_CT (got '$PORT_RAW')" >&2
      exit 1
    fi
    URL="http://localhost:${PORT}"
    local php_ready=false i
    for i in $(seq 1 30); do
      if docker exec "$WP_CT" php -r 'exit(0);' >/dev/null 2>&1; then
        php_ready=true
        break
      fi
      sleep 1
    done
    if [[ "$php_ready" != "true" ]]; then
      echo "test-env.sh: up FAILED — $WP_CT did not return after applying PHP upload limits" >&2
      exit 1
    fi
  fi
  upload_max="$(docker exec "$WP_CT" php -r 'echo ini_get("upload_max_filesize");')"
  if [[ "$upload_max" != "64M" ]]; then
    echo "test-env.sh: up FAILED — upload_max_filesize is '$upload_max', not 64M; theme ZIP uploads would silently fail" >&2
    exit 1
  fi

  echo "==> waiting for the database"
  wait_for_db "$DB_CT"

  if ! docker exec "$WP_CT" wp --allow-root core is-installed >/dev/null 2>&1; then
    echo "==> installing WordPress"
    docker exec "$WP_CT" wp --allow-root core install \
      --url="$URL" --title="html2wp test — $SLUG" \
      --admin_user=admin --admin_password=admin123 --admin_email=test@example.com \
      --skip-email --quiet
  else
    echo "==> WordPress already installed"
    # A Docker Desktop restart may have reassigned the published port. Core's
    # login redirects use these options, so stale values make a healthy admin
    # bounce to the previous dead port and look like bad credentials.
    docker exec "$WP_CT" wp --allow-root option update home "$URL" --quiet
    docker exec "$WP_CT" wp --allow-root option update siteurl "$URL" --quiet
  fi

  echo "==> setting permalink structure"
  docker exec "$WP_CT" wp --allow-root rewrite structure '/%postname%/' --hard
  docker exec "$WP_CT" wp --allow-root rewrite flush --hard

  # WordPress writes empty BEGIN/END markers when got_mod_rewrite() is false
  # in this container — that still "succeeds" and leaves every subpage
  # 404ing from Apache, so prove real rules are inside, not just that the
  # commands above didn't error.
  local rule_count
  rule_count="$(docker exec "$WP_CT" sh -c 'grep -c RewriteRule /var/www/html/.htaccess 2>/dev/null || echo 0')"
  if [[ "${rule_count:-0}" -lt 2 ]]; then
    echo "test-env.sh: up FAILED — .htaccess in $WP_CT has no real rewrite rules (got_mod_rewrite() false); every subpage would 404" >&2
    exit 1
  fi

  echo "==> fixing wp-content ownership"
  docker exec "$WP_CT" mkdir -p /var/www/html/wp-content/uploads
  docker exec "$WP_CT" chown -R www-data:www-data /var/www/html/wp-content

  # Prove it, rather than trusting that chown did what it claimed — the same
  # discipline tools/reset-test-wp.sh uses, for the same reason: a root-owned
  # uploads/ makes an admin theme upload fail with a bare HTTP 500.
  local owner
  owner="$(docker exec "$WP_CT" stat -c %U /var/www/html/wp-content/uploads 2>/dev/null || true)"
  if [[ "$owner" != "www-data" ]]; then
    echo "test-env.sh: up FAILED — wp-content/uploads is owned by '$owner', not www-data" >&2
    exit 1
  fi

  echo "==> deleting WordPress's sample content"
  docker exec "$WP_CT" sh -c '
    set -e
    ids=$(wp --allow-root post list --post_type=post,page --format=ids)
    if [ -n "$ids" ]; then wp --allow-root post delete $ids --force; fi
    cids=$(wp --allow-root comment list --format=ids)
    if [ -n "$cids" ]; then wp --allow-root comment delete $cids --force; fi
  '

  # WooCommerce, for a shop conversion only.
  #
  # Without it gate B/C cannot run at all on such a theme: the product pages
  # 404 (no `product` post type is registered), the importer skips the whole
  # catalogue by design, and C6 reads a Store API that is not there — so the
  # run reports a shop that imported nothing and looks like a conversion bug
  # rather than a missing plugin.
  #
  # Driven by the manifest rather than a flag, so nobody has to remember, and
  # gated on shop.present so a site without a shop gets the same container it
  # has always got. TEST_ENV_MANIFEST overrides the default lookup.
  local mf_path="${TEST_ENV_MANIFEST:-conversion-manifest.json}"
  if [[ -f "$mf_path" ]] && jq -e '.shop.present == true' "$mf_path" >/dev/null 2>&1; then
    if docker exec "$WP_CT" wp --allow-root plugin is-active woocommerce >/dev/null 2>&1; then
      echo "==> WooCommerce already active"
    else
      echo "==> installing WooCommerce (the manifest declares a shop)"
      docker exec "$WP_CT" wp --allow-root plugin install woocommerce --activate --quiet
      # Woo's own onboarding wizard hijacks wp-admin on first load and its
      # "coming soon" store mode answers the whole front end with a holding
      # page — which every pixel gate would then compare against the design.
      docker exec "$WP_CT" wp --allow-root option update woocommerce_onboarding_profile '{"skipped":true}' --format=json --quiet || true
      docker exec "$WP_CT" wp --allow-root option update woocommerce_task_list_hidden yes --quiet || true
      docker exec "$WP_CT" wp --allow-root option update woocommerce_coming_soon no --quiet || true
      # Cart/checkout/shop pages and the product permalink base exist only
      # after this; the redirect rows point at them.
      docker exec "$WP_CT" wp --allow-root wc --user=admin tool run install_pages >/dev/null 2>&1 || true
      docker exec "$WP_CT" wp --allow-root rewrite flush --hard
    fi
    if ! docker exec "$WP_CT" wp --allow-root plugin is-active woocommerce >/dev/null 2>&1; then
      echo "test-env.sh: up FAILED — the manifest declares a shop but WooCommerce could not be activated;" >&2
      echo "  every product page would 404 and gate C6 would read a Store API that is not there." >&2
      exit 1
    fi
  fi

  jq -n \
    --arg slug "$SLUG" \
    --arg project "$PROJECT" \
    --arg wpContainer "$WP_CT" \
    --arg dbContainer "$DB_CT" \
    --arg network "$NETWORK" \
    --argjson port "$PORT" \
    --arg url "$URL" \
    --arg wpCli "docker exec $WP_CT wp --allow-root" \
    --arg createdAt "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    '{slug:$slug, project:$project, wpContainer:$wpContainer, dbContainer:$dbContainer,
      network:$network, port:$port, url:$url, wpCli:$wpCli, createdAt:$createdAt}' \
    > "$state"

  echo "up ok"
  echo "  url:          $URL"
  echo "  wp container: $WP_CT"
  echo "  db container: $DB_CT"
  echo "  network:      $NETWORK"
  echo "  wp-cli:       docker exec $WP_CT wp --allow-root"
  echo "  state file:   $state"
}

check_cmd() {
  local slug="${1:?usage: test-env.sh check <slug> [expected-active-theme]}"
  local expect_theme="${2:-}"
  local SLUG state
  SLUG="$(sanitize_slug "$slug")"
  state="$(state_path "$SLUG")"

  if [[ ! -f "$state" ]]; then
    echo "check FAILED — no environment recorded for slug '$SLUG' (run 'up' first)" >&2
    exit 1
  fi

  local PROJECT WP_CT URL
  PROJECT="$(jq -r '.project' "$state")"
  WP_CT="$(jq -r '.wpContainer' "$state")"
  URL="$(jq -r '.url' "$state")"
  require_safe_project "$PROJECT"
  require_safe_project "$WP_CT"

  local running
  running="$(docker inspect -f '{{.State.Running}}' "$WP_CT" 2>/dev/null || echo false)"
  if [[ "$running" != "true" ]]; then
    echo "check FAILED — $WP_CT is not running" >&2
    exit 1
  fi

  local label_project
  label_project="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' "$WP_CT" 2>/dev/null || true)"
  if [[ "$label_project" != "$PROJECT" ]]; then
    echo "check FAILED — $WP_CT belongs to project '$label_project', expected '$PROJECT'" >&2
    exit 1
  fi

  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$URL/" || echo 000)"
  case "$code" in
    2??|3??) ;;
    *) echo "check FAILED — $URL responded with HTTP $code" >&2; exit 1 ;;
  esac

  if [[ -n "$expect_theme" ]]; then
    local active
    active="$(docker exec "$WP_CT" wp --allow-root theme list --status=active --field=name 2>/dev/null || true)"
    if [[ "$active" != "$expect_theme" ]]; then
      echo "check FAILED — active theme is '${active:-<none>}', expected '$expect_theme'" >&2
      exit 1
    fi
  fi

  echo "check ok — $URL up (project=$PROJECT, container=$WP_CT)${expect_theme:+, theme=$expect_theme}"
}

down_cmd() {
  local slug="${1:?usage: test-env.sh down <slug>}"
  local SLUG state
  SLUG="$(sanitize_slug "$slug")"
  state="$(state_path "$SLUG")"

  # A missing state file used to mean "nothing to tear down", exit 0. It does
  # not: the state file is only this script's memory, and the containers are
  # docker's. After wave 1 the worktrees were removed, the project name carries
  # a hash of the worktree path, so `down` computed a different name, matched
  # nothing, and reported success — while six containers and three WordPress
  # instances kept running until someone happened to look.
  #
  # That is the family this batch exists to end: a step reporting work it did
  # not do. So the question goes to DOCKER, which knows, instead of to a file
  # that can be stale.
  if [[ ! -f "$state" ]]; then
    local orphans
    orphans="$(docker ps -a --filter "label=com.docker.compose.project" \
                 --format '{{.Label "com.docker.compose.project"}}' 2>/dev/null \
               | sort -u | grep -E "^h2wp-${SLUG}-" || true)"
    if [[ -z "$orphans" ]]; then
      echo "down — nothing to tear down for slug '$SLUG': no state file at $state and no running project matches h2wp-${SLUG}-*"
      exit 0
    fi
    echo "down — no state file at $state, but docker still has project(s) for this slug:" >&2
    local n=0
    while IFS= read -r proj; do
      [[ -z "$proj" ]] && continue
      require_safe_project "$proj"
      echo "  tearing down $proj" >&2
      # `compose down` EXITS 0 on a project whose compose file has moved —
      # it prints "No resource found to remove" to stderr and leaves every
      # container running. Chaining the fallback on its exit code therefore
      # never fires. Measured while writing this fix, which is the same bug
      # one layer up: check the RESULT, never the return code.
      docker compose -p "$proj" down -v --remove-orphans >/dev/null 2>&1 || true
      if [[ -n "$(docker ps -aq --filter "label=com.docker.compose.project=$proj" 2>/dev/null)" ]]; then
        docker rm -f $(docker ps -aq --filter "label=com.docker.compose.project=$proj") >/dev/null 2>&1 || true
        docker volume rm -f $(docker volume ls -q --filter "label=com.docker.compose.project=$proj" 2>/dev/null) >/dev/null 2>&1 || true
        docker network rm $(docker network ls -q --filter "label=com.docker.compose.project=$proj" 2>/dev/null) >/dev/null 2>&1 || true
      fi
      local still
      still="$(docker ps -aq --filter "label=com.docker.compose.project=$proj" 2>/dev/null | wc -l | tr -d ' ')"
      if [[ "$still" != "0" ]]; then
        echo "down FAILED — $still container(s) of orphaned project '$proj' survived teardown" >&2
        exit 1
      fi
      n=$((n + 1))
    done <<< "$orphans"
    echo "down ok — removed $n orphaned project(s) for slug '$SLUG'"
    exit 0
  fi

  PROJECT="$(jq -r '.project' "$state")"
  require_safe_project "$PROJECT"

  compose down -v --remove-orphans
  rm -f "$state"

  # Verify rather than announce. `compose down` is quiet about a project whose
  # compose file has moved out from under it, and this line is the only thing
  # a caller reads.
  local left
  left="$(docker ps -aq --filter "label=com.docker.compose.project=$PROJECT" 2>/dev/null | wc -l | tr -d ' ')"
  if [[ "$left" != "0" ]]; then
    echo "down FAILED — $left container(s) of project '$PROJECT' are still there after compose down" >&2
    exit 1
  fi
  echo "down ok — removed containers, volumes and network for project '$PROJECT'"
}

CMD="${1:-}"
[[ $# -gt 0 ]] && shift

case "$CMD" in
  up) up_cmd "$@" ;;
  check) check_cmd "$@" ;;
  down) down_cmd "$@" ;;
  *)
    echo "usage: test-env.sh <up|check|down> <slug> [args]" >&2
    exit 2
    ;;
esac

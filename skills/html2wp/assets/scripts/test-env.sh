#!/usr/bin/env bash
# Isolated, per-run WordPress test environment for html2wp-sub, stage 5.
#
#   test-env.sh up <slug>            create/reuse a throwaway WP for <slug>
#   test-env.sh check <slug> [theme] assert it is still that same environment
#   test-env.sh reset <slug>         put it back exactly as `up` left it (no restart)
#   test-env.sh clone <slug> <copy>  a second WordPress, a byte-copy of <slug> now
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
#
# `reset` exists because stage 5 installs the theme more than once per
# conversion (install #1 → editor smoke, which WRITES into the site → install
# #2, the one that is measured and handed over), and `down` + `up` pays for a
# new WordPress, wp-cli, a container restart and — on a shop — a WooCommerce
# download every time. The `up` that ran `wp core install` snapshots the
# finished clean site once, inside the containers; `reset` restores that
# snapshot and then PROVES it: database checksums, the docroot's file tree,
# and a clean-state assertion, each compared against what was recorded when
# the snapshot was taken. A reset that cannot prove it is clean fails.
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

read_port() {
  local raw port
  raw="$(compose port wp 80)"
  port="${raw##*:}"
  if [[ -z "$port" || "$port" == "$raw" ]]; then
    echo "test-env.sh: could not read the published port for project $PROJECT (got '$raw')" >&2
    return 1
  fi
  printf '%s' "$port"
}

# WordPress writes empty BEGIN/END markers when got_mod_rewrite() is false
# in this container — that still "succeeds" and leaves every subpage
# 404ing from Apache, so prove real rules are inside, not just that the
# flush didn't error. Used by `up` and by `reset`.
assert_rewrite_rules() {
  local wp_ct="$1" what="$2" rule_count
  rule_count="$(docker exec "$wp_ct" sh -c 'grep -c RewriteRule /var/www/html/.htaccess 2>/dev/null || echo 0')"
  if [[ "${rule_count:-0}" -lt 2 ]]; then
    echo "test-env.sh: $what FAILED — .htaccess in $wp_ct has no real rewrite rules (got_mod_rewrite() false); every subpage would 404" >&2
    exit 1
  fi
}

# Prove it, rather than trusting that chown did what it claimed — the same
# discipline tools/reset-test-wp.sh uses, for the same reason: a root-owned
# uploads/ makes an admin theme upload fail with a bare HTTP 500.
fix_wp_content_ownership() {
  local wp_ct="$1" what="$2" owner
  docker exec "$wp_ct" mkdir -p /var/www/html/wp-content/uploads
  docker exec "$wp_ct" chown -R www-data:www-data /var/www/html/wp-content
  owner="$(docker exec "$wp_ct" stat -c %U /var/www/html/wp-content/uploads 2>/dev/null || true)"
  if [[ "$owner" != "www-data" ]]; then
    echo "test-env.sh: $what FAILED — wp-content/uploads is owned by '$owner', not www-data" >&2
    exit 1
  fi
}

# --- snapshot / reset -------------------------------------------------------
#
# Where the snapshot lives: INSIDE each container, outside the WordPress
# volume, so restoring the docroot can never delete it and nothing of it is
# reachable over HTTP. It is in the container's writable layer, so it lives
# exactly as long as the container — a container that compose recreates has
# no snapshot, and `reset` says so instead of restoring half of one.
SNAP_DIR=/var/lib/h2wp-snapshot

# `up`'s sample-content cleanup (run with `wp eval`). WordPress's stock sample
# is exactly three items — post `hello-world` (its sample comment is deleted
# with it), page `sample-page`, draft page `privacy-policy` — and a copy is deleted only while nothing claims it: an
# importer stamps what it created (_clara_ve_key / _clara_ve_theme /
# _html2wp_bundle_file on the HTML theme, _h2wp_gb_source on the Gutenberg
# one), and a converted site can have its own Privacy Policy page. Everything
# else — imported pages, posts, products, an owner's own page — is never
# touched. Prints what it deleted.
SAMPLE_CONTENT_PHP='
$stock = array( "post" => array( "hello-world" ), "page" => array( "sample-page", "privacy-policy" ) );
$owners = array( "_clara_ve_key", "_clara_ve_theme", "_html2wp_bundle_file", "_h2wp_gb_source" );
$gone = array();
foreach ( $stock as $type => $names ) {
  foreach ( get_posts( array( "post_type" => $type, "post_name__in" => $names, "post_status" => "any", "numberposts" => -1 ) ) as $p ) {
    $claimed = false;
    foreach ( $owners as $k ) { if ( metadata_exists( "post", $p->ID, $k ) ) { $claimed = true; } }
    if ( $claimed ) { continue; }
    wp_delete_post( $p->ID, true );
    $gone[] = $type . ":" . $p->post_name;
  }
}
echo $gone ? "deleted " . implode( ", ", $gone ) . "\n" : "no stock sample content left\n";
'

# Per-table CHECKSUM TABLE for database $2, as sorted "table<TAB>checksum"
# lines with the database name stripped, so a scratch copy and the live
# database are comparable.
db_checksums() {
  local db_ct="$1" db="$2"
  docker exec -e PW="$DB_ROOT_PW" -e DB="$db" "$db_ct" sh -c '
    set -e
    tables=$(mariadb -uroot -p"$PW" -N -e "SET SESSION group_concat_max_len = 1048576; SELECT GROUP_CONCAT(CONCAT(\"\`\", table_name, \"\`\") ORDER BY table_name) FROM information_schema.tables WHERE table_schema=\"$DB\"")
    [ -n "$tables" ] && [ "$tables" != "NULL" ] || { echo "no tables in $DB" >&2; exit 1; }
    mariadb -uroot -p"$PW" -N -D "$DB" -e "CHECKSUM TABLE $tables EXTENDED" | sed "s/^$DB\.//" | LC_ALL=C sort
  '
}

# One sha1 over every file (and symlink) under the docroot, path-sorted.
# wp-content/uploads/wc-logs is left out: WooCommerce writes dated log files
# there on its own whenever it loads — including under the wp-cli calls
# `reset` itself makes between restoring and measuring — so hashing it would
# turn a clean reset into a mismatch on a shop's environment.
docroot_tree_sha1() {
  docker exec "$1" sh -c '
    cd /var/www/html &&
    find . -path ./wp-content/uploads/wc-logs -prune -o \( -type f -o -type l \) -print0 \
      | LC_ALL=C sort -z | xargs -0r sha1sum | sha1sum | cut -d" " -f1'
}

# The clean state every install starts from: the theme `up` left active,
# no active plugin but WooCommerce, and none of the options a converted
# theme, its importer or the editor writes. Used before taking the snapshot
# (never snapshot a dirty site) and after every reset.
assert_clean_state() {
  local wp_ct="$1" expect_theme="$2" what="$3" active bad pat hits
  active="$(docker exec "$wp_ct" wp --allow-root theme list --status=active --field=name)"
  if [[ -n "$expect_theme" && "$active" != "$expect_theme" ]]; then
    echo "test-env.sh: $what FAILED — active theme is '${active:-<none>}', expected '$expect_theme'" >&2
    exit 1
  fi
  # --status=active: a fresh WordPress ships akismet and hello-dolly
  # INSTALLED but inactive; they are not state a conversion left behind.
  bad="$(docker exec "$wp_ct" wp --allow-root plugin list --status=active --field=name | grep -vx 'woocommerce' || true)"
  if [[ -n "$bad" ]]; then
    echo "test-env.sh: $what FAILED — plugin(s) still active besides woocommerce: $(echo $bad)" >&2
    exit 1
  fi
  for pat in '*_theme_import_state' 'clara_ve_*' 'html2wp_*'; do
    hits="$(docker exec "$wp_ct" wp --allow-root option list --search="$pat" --field=option_name)"
    if [[ -n "$hits" ]]; then
      echo "test-env.sh: $what FAILED — option(s) matching '$pat' are present: $(echo $hits)" >&2
      exit 1
    fi
  done
}

# Takes the snapshot and leaves its JSON record in SNAPSHOT_JSON. Not printed
# for the caller to capture: a function run inside $(...) does not inherit
# `set -e` (bash without inherit_errexit), so a failed step in here would
# have carried on and recorded a snapshot with empty fields.
SNAPSHOT_JSON=""
take_snapshot() {
  local wp_ct="$1" db_ct="$2"
  echo "==> snapshotting the clean install (what 'reset' restores)" >&2
  local theme
  theme="$(docker exec "$wp_ct" wp --allow-root theme list --status=active --field=name)"
  assert_clean_state "$wp_ct" "" "snapshot" >&2

  # Database. Dumped WITHOUT --databases, so it restores into any name —
  # first into a scratch database right here, so the checksums recorded are
  # those of a RESTORED copy. That is both the proof the dump restores at
  # all, and the only reference a later restore can match exactly: the live
  # database is not what a restore reproduces (a dump rebuilds tables).
  local charset collation
  read -r charset collation < <(docker exec "$db_ct" mariadb -uroot -p"$DB_ROOT_PW" -N -e \
    "SELECT DEFAULT_CHARACTER_SET_NAME, DEFAULT_COLLATION_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME='wordpress'")
  if [[ -z "${charset:-}" || -z "${collation:-}" ]]; then
    echo "test-env.sh: snapshot FAILED — could not read the wordpress database's charset/collation from $db_ct" >&2
    exit 1
  fi
  docker exec -e PW="$DB_ROOT_PW" -e D="$SNAP_DIR" -e CS="$charset" -e CO="$collation" "$db_ct" sh -c '
    set -e
    mkdir -p "$D"
    mariadb-dump -uroot -p"$PW" --single-transaction --routines --triggers --events wordpress > "$D/db.sql"
    mariadb -uroot -p"$PW" -e "DROP DATABASE IF EXISTS wordpress_snapcheck; CREATE DATABASE wordpress_snapcheck CHARACTER SET $CS COLLATE $CO"
    mariadb -uroot -p"$PW" wordpress_snapcheck < "$D/db.sql"
  ' >&2
  local checksums
  checksums="$(db_checksums "$db_ct" wordpress_snapcheck)"
  docker exec "$db_ct" mariadb -uroot -p"$DB_ROOT_PW" -e "DROP DATABASE wordpress_snapcheck"
  if [[ -z "$checksums" ]]; then
    echo "test-env.sh: snapshot FAILED — the restored copy of the database has no table checksums" >&2
    exit 1
  fi

  # Files: the WHOLE docroot, not only wp-content — WordPress rewrites
  # .htaccess, and a theme or plugin can write next to wp-config.php.
  docker exec -e D="$SNAP_DIR" "$wp_ct" sh -c 'set -e; mkdir -p "$D"; tar -C /var/www/html -cpf "$D/docroot.tar" .' >&2
  local tree tar_sha dump_sha wp_version woo_version
  tree="$(docroot_tree_sha1 "$wp_ct")"
  tar_sha="$(docker exec "$wp_ct" sha1sum "$SNAP_DIR/docroot.tar" | cut -d' ' -f1)"
  dump_sha="$(docker exec "$db_ct" sha1sum "$SNAP_DIR/db.sql" | cut -d' ' -f1)"
  wp_version="$(docker exec "$wp_ct" wp --allow-root core version)"
  woo_version="$(docker exec "$wp_ct" wp --allow-root plugin get woocommerce --field=version 2>/dev/null || true)"

  SNAPSHOT_JSON="$(jq -c -n \
    --arg dir "$SNAP_DIR" --arg theme "$theme" \
    --arg charset "$charset" --arg collation "$collation" \
    --arg checksums "$checksums" --arg tree "$tree" \
    --arg tarSha1 "$tar_sha" --arg dumpSha1 "$dump_sha" \
    --arg wp "$wp_version" --arg woo "$woo_version" \
    --arg takenAt "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    '{dir:$dir, defaultTheme:$theme, dbCharset:$charset, dbCollation:$collation,
      dbChecksums:$checksums, docrootTreeSha1:$tree, docrootTarSha1:$tarSha1, dbDumpSha1:$dumpSha1,
      wpVersion:$wp, wooVersion:(if $woo == "" then null else $woo end), takenAt:$takenAt}')"
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

# Tear down one compose project of the h2wp- family, and CHECK that it went
# (see down_cmd for why the exit code of `compose down` proves nothing).
teardown_project() {
  local proj="$1" still
  require_safe_project "$proj"
  docker compose -p "$proj" down -v --remove-orphans >/dev/null 2>&1 || true
  if [[ -n "$(docker ps -aq --filter "label=com.docker.compose.project=$proj" 2>/dev/null)" ]]; then
    docker rm -f $(docker ps -aq --filter "label=com.docker.compose.project=$proj") >/dev/null 2>&1 || true
    docker volume rm -f $(docker volume ls -q --filter "label=com.docker.compose.project=$proj" 2>/dev/null) >/dev/null 2>&1 || true
    docker network rm $(docker network ls -q --filter "label=com.docker.compose.project=$proj" 2>/dev/null) >/dev/null 2>&1 || true
  fi
  still="$(docker ps -aq --filter "label=com.docker.compose.project=$proj" 2>/dev/null | wc -l | tr -d ' ')"
  [[ "$still" == "0" ]]
}

# Projects docker still runs for this slug (exact h2wp-<slug>-<6 hex>).
projects_for_slug() {
  docker ps -a --filter "label=com.docker.compose.project" \
    --format '{{.Label "com.docker.compose.project"}}' 2>/dev/null \
    | sort -u | grep -E "^h2wp-${1}-[0-9a-f]{6}$" || true
}

up_cmd() {
  local slug="${1:?usage: test-env.sh up <slug>}"
  local SLUG state
  SLUG="$(sanitize_slug "$slug")"
  state="$(state_path "$SLUG")"

  # The snapshot record survives a re-run of `up` (the final state write
  # below replaces the whole file, so it has to be carried over explicitly).
  local old_snapshot="null" fresh_install=false woo_installed_now=false
  if [[ -f "$state" ]]; then
    PROJECT="$(jq -r '.project' "$state")"
    old_snapshot="$(jq -c '.snapshot // null' "$state")"
    echo "==> reusing existing run for '$SLUG': project=$PROJECT"
  else
    # No state file — but maybe a run of THIS slug from THIS workspace is
    # still up: the state file was deleted (a pipeline script clearing its
    # workspace), or the workspace was copied in afresh over the same path.
    # Every new `up` then started another project and left the old one
    # running — found live, eight WordPress stacks where two were in use,
    # until the disk filled. Each run carries the state file it belongs to as
    # a container label (test-env-compose.yml), so a run whose label is THIS
    # state file is ours, and is torn down before the new one starts. A run
    # of the same slug from another workspace has another label and is never
    # touched.
    local proj owner
    while IFS= read -r proj; do
      [[ -z "$proj" ]] && continue
      owner="$(docker ps -a --filter "label=com.docker.compose.project=$proj" --format '{{.Label "h2wp.state"}}' 2>/dev/null | head -1)"
      if [[ "$owner" == "$state" ]]; then
        echo "==> an earlier run of '$SLUG' from this workspace lost its state file — removing $proj"
        teardown_project "$proj" || { echo "test-env.sh: up FAILED — could not remove the orphaned project $proj" >&2; exit 1; }
      fi
    done <<< "$(projects_for_slug "$SLUG")"
    PROJECT="h2wp-${SLUG}-$(gen_run_id)"
    echo "==> new run for '$SLUG': project=$PROJECT"
  fi
  require_safe_project "$PROJECT"

  echo "==> docker compose up -d"
  H2WP_STATE_FILE="$state" compose up -d

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
    fresh_install=true
  else
    echo "==> WordPress already installed"
    # A Docker Desktop restart may have reassigned the published port. Core's
    # login redirects use these options, so stale values make a healthy admin
    # bounce to the previous dead port and look like bad credentials.
    docker exec "$WP_CT" wp --allow-root option update home "$URL" --quiet
    docker exec "$WP_CT" wp --allow-root option update siteurl "$URL" --quiet
  fi

  # WordPress's OWN default structure (6.7+), not /%postname%/. Pretty
  # permalinks are needed so routing and the .htaccess proof below work, but
  # the theme's importer applies the bundle's structure only over a WordPress
  # default — which is what an owner's fresh site has. Setting /%postname%/
  # here made it look like an owner's choice, so the bundle's structure (e.g.
  # /blog/%postname%/, keeping the articles' original addresses) was never
  # exercised in any test and every gate measured a different site.
  #
  # Only on a WordPress nobody has set one on yet. `up` is re-run on an env a
  # theme was already installed on (stage3-remote runs it on every pass), and
  # the importer has by then applied the bundle's own structure — resetting it
  # moved every imported post to a date URL. An empty structure is also what
  # a run that crashed between install and this step leaves, so that is
  # still repaired.
  current_structure="$(docker exec "$WP_CT" wp --allow-root option get permalink_structure 2>/dev/null || true)"
  if [[ "$fresh_install" == "true" || -z "$current_structure" ]]; then
    echo "==> setting permalink structure (WordPress's default, as a fresh install has)"
    docker exec "$WP_CT" wp --allow-root rewrite structure '/%year%/%monthnum%/%day%/%postname%/' --hard
  else
    echo "==> keeping the permalink structure already set ($current_structure)"
  fi
  docker exec "$WP_CT" wp --allow-root rewrite flush --hard
  assert_rewrite_rules "$WP_CT" up

  echo "==> fixing wp-content ownership"
  fix_wp_content_ownership "$WP_CT" up

  # WordPress's STOCK sample content only — the "Hello world!" post (its
  # comment goes with it), the Sample Page and the draft Privacy Policy — and
  # only a copy no importer has claimed. This used to delete every post and
  # page, which on a re-run of `up` over an installed theme (stage3-remote
  # runs `up` on every pass) wiped the whole imported site: every page 404'd
  # and the next gate run measured an empty WordPress.
  echo "==> deleting WordPress's sample content"
  docker exec "$WP_CT" wp --allow-root eval "$SAMPLE_CONTENT_PHP"

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
      # Idempotent: creates only the Woo pages that are missing.
      docker exec "$WP_CT" wp --allow-root wc --user=admin tool run install_pages >/dev/null 2>&1 || true
    else
      echo "==> installing WooCommerce (the manifest declares a shop)"
      woo_installed_now=true
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
      # wp-cli runs as root, so the plugin install just left a root-owned
      # wp-content/upgrade/ (and Woo's own uploads dirs) AFTER the chown
      # above. The admin's theme upload then fails with "Could not create
      # directory …/wp-content/upgrade/<theme>" — measured on a shop env.
      fix_wp_content_ownership "$WP_CT" up
    fi
    if ! docker exec "$WP_CT" wp --allow-root plugin is-active woocommerce >/dev/null 2>&1; then
      echo "test-env.sh: up FAILED — the manifest declares a shop but WooCommerce could not be activated;" >&2
      echo "  every product page would 404 and gate C6 would read a Store API that is not there." >&2
      exit 1
    fi
  fi

  # The snapshot `reset` restores. Taken ONLY by the run that installed
  # WordPress, at its very end: a re-run of `up` may be pointing at a site a
  # theme was already installed into, and snapshotting that would make every
  # later reset restore someone's half-finished install as "clean". (The
  # clean-state assertion inside take_snapshot would catch most of it — this
  # does not rely on that.) A WooCommerce install by a LATER run changes the
  # clean site the snapshot describes, so it invalidates the snapshot instead
  # of silently resetting to a shop-less WordPress; `reset` then refuses and
  # names `down` + `up` as the way back.
  local snapshot="null"
  if [[ "$fresh_install" == "true" ]]; then
    take_snapshot "$WP_CT" "$DB_CT"
    snapshot="$SNAPSHOT_JSON"
  elif [[ "$woo_installed_now" == "true" && "$old_snapshot" != "null" ]]; then
    echo "==> WooCommerce was installed after the snapshot — snapshot invalidated; 'reset' is unavailable until 'down' + 'up'"
    docker exec "$WP_CT" rm -rf "$SNAP_DIR" || true
    docker exec "$DB_CT" rm -rf "$SNAP_DIR" || true
  else
    snapshot="$old_snapshot"
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
    --argjson snapshot "$snapshot" \
    '{slug:$slug, project:$project, wpContainer:$wpContainer, dbContainer:$dbContainer,
      network:$network, port:$port, url:$url, wpCli:$wpCli, createdAt:$createdAt,
      snapshot:$snapshot}' \
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

# Put the environment back exactly as `up` left it, without restarting a
# container (a restart can move Docker Desktop's published port — the reason
# `up` re-reads it after its own restart). Order: files → database → prove
# the database → home/siteurl → ownership → rewrite rules → prove the files
# → prove the clean state. Every proof compares against the record `up`
# stored when it took the snapshot; any mismatch fails the reset rather than
# handing a gate a site that only looks fresh.
reset_cmd() {
  local slug="${1:?usage: test-env.sh reset <slug>}"
  local SLUG state
  SLUG="$(sanitize_slug "$slug")"
  state="$(state_path "$SLUG")"

  if [[ ! -f "$state" ]]; then
    echo "reset FAILED — no environment recorded for slug '$SLUG' (run 'up' first)" >&2
    exit 1
  fi

  local WP_CT DB_CT snap
  PROJECT="$(jq -r '.project' "$state")"
  WP_CT="$(jq -r '.wpContainer' "$state")"
  DB_CT="$(jq -r '.dbContainer' "$state")"
  snap="$(jq -c '.snapshot // null' "$state")"
  require_safe_project "$PROJECT"
  require_safe_project "$WP_CT"
  require_safe_project "$DB_CT"

  if [[ "$snap" == "null" ]]; then
    echo "reset FAILED — '$SLUG' has no valid snapshot. Only the 'up' that installs WordPress takes one (a" >&2
    echo "  crash before its end, or a later 'up' that installed WooCommerce, leaves none)." >&2
    echo "  Use 'test-env.sh down $SLUG' then 'test-env.sh up $SLUG'." >&2
    exit 1
  fi

  local ct
  for ct in "$WP_CT" "$DB_CT"; do
    if [[ "$(docker inspect -f '{{.State.Running}}' "$ct" 2>/dev/null || echo false)" != "true" ]]; then
      echo "reset FAILED — $ct is not running" >&2
      exit 1
    fi
  done

  # The snapshot files must be the ones recorded — a recreated container has
  # none, and a partial one must never be restored.
  local want got
  want="$(jq -r '.snapshot.docrootTarSha1' "$state")"
  got="$(docker exec "$WP_CT" sh -c "sha1sum $SNAP_DIR/docroot.tar 2>/dev/null | cut -d' ' -f1" || true)"
  if [[ -z "$got" || "$got" != "$want" ]]; then
    echo "reset FAILED — the docroot snapshot in $WP_CT is missing or changed (sha1 '${got:-none}', recorded '$want'); use down + up" >&2
    exit 1
  fi
  want="$(jq -r '.snapshot.dbDumpSha1' "$state")"
  got="$(docker exec "$DB_CT" sh -c "sha1sum $SNAP_DIR/db.sql 2>/dev/null | cut -d' ' -f1" || true)"
  if [[ -z "$got" || "$got" != "$want" ]]; then
    echo "reset FAILED — the database snapshot in $DB_CT is missing or changed (sha1 '${got:-none}', recorded '$want'); use down + up" >&2
    exit 1
  fi

  echo "==> restoring the docroot"
  # -mindepth 1 -delete, not rm -rf /var/www/html/*: it also removes the
  # dotfiles (.htaccess, and whatever a plugin hid), and never the directory
  # itself, which is the volume's mount point.
  docker exec -e D="$SNAP_DIR" "$WP_CT" sh -c 'set -e; find /var/www/html -mindepth 1 -delete; tar -C /var/www/html -xpf "$D/docroot.tar"'

  echo "==> restoring the database"
  # DROP + CREATE, not a plain re-import over the live database: the dump's
  # DROP TABLE lines only cover the tables it has, so every table a theme or
  # plugin created since (the editor's, WooCommerce's when it came later)
  # would survive. The wordpress user's grants are on `wordpress`.* and
  # outlive the drop.
  docker exec -e PW="$DB_ROOT_PW" -e D="$SNAP_DIR" \
    -e CS="$(jq -r '.snapshot.dbCharset' "$state")" -e CO="$(jq -r '.snapshot.dbCollation' "$state")" \
    "$DB_CT" sh -c '
      set -e
      mariadb -uroot -p"$PW" -e "DROP DATABASE wordpress; CREATE DATABASE wordpress CHARACTER SET $CS COLLATE $CO"
      mariadb -uroot -p"$PW" wordpress < "$D/db.sql"
    '
  local checksums
  checksums="$(db_checksums "$DB_CT" wordpress)"
  if [[ "$checksums" != "$(jq -r '.snapshot.dbChecksums' "$state")" ]]; then
    echo "reset FAILED — the restored database's table checksums differ from the snapshot's:" >&2
    diff <(jq -r '.snapshot.dbChecksums' "$state") <(printf '%s\n' "$checksums") >&2 || true
    exit 1
  fi

  # Docker may have moved the published port since `up` (a Docker Desktop
  # restart). Re-read it and write it into the options and the state file,
  # so later stages reading `.url` do not target a dead port.
  local PORT URL
  PORT="$(read_port)"
  URL="http://localhost:${PORT}"
  docker exec "$WP_CT" wp --allow-root option update home "$URL" --quiet
  docker exec "$WP_CT" wp --allow-root option update siteurl "$URL" --quiet
  if [[ "$URL" != "$(jq -r '.url' "$state")" ]]; then
    echo "==> published port moved; state file now says $URL"
    local tmp="$state.tmp"
    jq --argjson port "$PORT" --arg url "$URL" '.port = $port | .url = $url' "$state" > "$tmp" && mv "$tmp" "$state"
  fi

  fix_wp_content_ownership "$WP_CT" reset
  docker exec "$WP_CT" wp --allow-root rewrite flush --hard
  assert_rewrite_rules "$WP_CT" reset

  local tree
  tree="$(docroot_tree_sha1 "$WP_CT")"
  if [[ "$tree" != "$(jq -r '.snapshot.docrootTreeSha1' "$state")" ]]; then
    echo "reset FAILED — the docroot's file tree differs from the snapshot's (sha1 $tree, recorded $(jq -r '.snapshot.docrootTreeSha1' "$state"))" >&2
    exit 1
  fi

  assert_clean_state "$WP_CT" "$(jq -r '.snapshot.defaultTheme' "$state")" reset
  local wp_version woo_version
  wp_version="$(docker exec "$WP_CT" wp --allow-root core version)"
  woo_version="$(docker exec "$WP_CT" wp --allow-root plugin get woocommerce --field=version 2>/dev/null || true)"
  if [[ "$wp_version" != "$(jq -r '.snapshot.wpVersion' "$state")" \
        || "$woo_version" != "$(jq -r '.snapshot.wooVersion // ""' "$state")" ]]; then
    echo "reset FAILED — WordPress/WooCommerce versions after reset ($wp_version / ${woo_version:-none}) are not the snapshot's" >&2
    exit 1
  fi

  echo "reset ok — $URL is the clean install again (snapshot $(jq -r '.snapshot.takenAt' "$state"); db checksums, docroot tree and clean state verified)"
}

# A second WordPress that is a byte-copy of <slug>'s CURRENT state — its
# database and its whole docroot — on its own compose project, containers,
# volumes and port, with its own state file.
#
# Why: the editor smoke test WRITES into the site (and its restore is
# best-effort), while gates B/C only read. On one WordPress they have to run
# one after the other and the measured install has to come after a reset;
# with a clone the smoke test writes into the copy while the gates read the
# original at the same time, and the original is never written to.
#
# The copy is taken while nothing is writing to the source (run it right
# after the install, before any gate starts): a consistent dump is
# --single-transaction, but the docroot tar is not atomic with it.
#
# The clone has no snapshot, so `reset` refuses on it — it is meant to be
# thrown away with `down <clone-slug>`, which cannot touch the original
# (different compose project; see require_safe_project and down's exact
# project match).
clone_cmd() {
  local src_slug="${1:?usage: test-env.sh clone <slug> <clone-slug>}"
  local dst_slug="${2:?usage: test-env.sh clone <slug> <clone-slug>}"
  local SRC DST src_state state
  SRC="$(sanitize_slug "$src_slug")"
  DST="$(sanitize_slug "$dst_slug")"
  src_state="$(state_path "$SRC")"
  state="$(state_path "$DST")"
  if [[ "$SRC" == "$DST" ]]; then
    echo "clone FAILED — the clone needs its own slug, not '$SRC'" >&2
    exit 2
  fi
  if [[ ! -f "$src_state" ]]; then
    echo "clone FAILED — no environment recorded for slug '$SRC' (run 'up' first)" >&2
    exit 1
  fi
  # Refused rather than reused: `up`'s reuse-on-rerun would be wrong here —
  # an existing clone holds whatever the smoke test wrote into it.
  if [[ -f "$state" ]]; then
    echo "clone FAILED — '$DST' already has a state file ($state); 'down $DST' first" >&2
    exit 1
  fi

  local SRC_PROJECT SRC_WP SRC_DB SRC_URL
  SRC_PROJECT="$(jq -r '.project' "$src_state")"
  SRC_WP="$(jq -r '.wpContainer' "$src_state")"
  SRC_DB="$(jq -r '.dbContainer' "$src_state")"
  SRC_URL="$(jq -r '.url' "$src_state")"
  require_safe_project "$SRC_PROJECT"
  require_safe_project "$SRC_WP"
  require_safe_project "$SRC_DB"
  local ct
  for ct in "$SRC_WP" "$SRC_DB"; do
    if [[ "$(docker inspect -f '{{.State.Running}}' "$ct" 2>/dev/null || echo false)" != "true" ]]; then
      echo "clone FAILED — source container $ct is not running" >&2
      exit 1
    fi
  done

  PROJECT="h2wp-${DST}-$(gen_run_id)"
  require_safe_project "$PROJECT"
  echo "==> new clone '$DST' of '$SRC': project=$PROJECT"
  H2WP_STATE_FILE="$state" compose up -d

  local WP_CT DB_CT
  WP_CT="$(resolve_container_name wp)"
  DB_CT="$(resolve_container_name db)"
  require_safe_project "$WP_CT"
  require_safe_project "$DB_CT"

  # The image entrypoint copies WordPress into the empty volume on first
  # start and only then execs Apache. Replacing the docroot while that copy
  # is still running would interleave the two, so wait for Apache to answer.
  local i ready=false
  for i in $(seq 1 60); do
    if docker exec "$WP_CT" php -r 'exit(@fsockopen("127.0.0.1", 80) ? 0 : 1);' >/dev/null 2>&1; then
      ready=true
      break
    fi
    sleep 1
  done
  if [[ "$ready" != "true" ]]; then
    echo "clone FAILED — Apache in $WP_CT never came up" >&2
    exit 1
  fi

  # Same PHP limits as the source (see up_cmd), and the same restart. Done
  # BEFORE the port is read, because the restart can move it.
  { docker exec "$SRC_WP" cat /usr/local/etc/php/conf.d/uploads.ini 2>/dev/null || true; } \
    | docker exec -i "$WP_CT" sh -c 'cat > /usr/local/etc/php/conf.d/uploads.ini'
  docker restart "$WP_CT" >/dev/null
  for i in $(seq 1 30); do
    docker exec "$WP_CT" php -r 'exit(@fsockopen("127.0.0.1", 80) ? 0 : 1);' >/dev/null 2>&1 && break
    sleep 1
  done
  ensure_wp_cli "$WP_CT"

  local PORT URL NETWORK
  PORT="$(read_port)"
  URL="http://localhost:${PORT}"
  NETWORK="${PROJECT}_default"

  echo "==> copying the docroot from $SRC_WP"
  # Streamed container to container; wp-config.php comes with it and still
  # fits, because both compose projects hand WordPress the same DB host
  # (`db`, resolved inside each project's own network) and credentials.
  docker exec "$SRC_WP" tar -C /var/www/html -cpf - . \
    | docker exec -i "$WP_CT" sh -c 'set -e; find /var/www/html -mindepth 1 -delete; tar -C /var/www/html -xpf -'

  echo "==> copying the database from $SRC_DB"
  wait_for_db "$DB_CT"
  local charset collation
  read -r charset collation < <(docker exec "$SRC_DB" mariadb -uroot -p"$DB_ROOT_PW" -N -e \
    "SELECT DEFAULT_CHARACTER_SET_NAME, DEFAULT_COLLATION_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME='wordpress'")
  docker exec -e PW="$DB_ROOT_PW" -e CS="$charset" -e CO="$collation" "$DB_CT" sh -c \
    'mariadb -uroot -p"$PW" -e "DROP DATABASE wordpress; CREATE DATABASE wordpress CHARACTER SET $CS COLLATE $CO"'
  docker exec -e PW="$DB_ROOT_PW" "$SRC_DB" sh -c \
    'mariadb-dump -uroot -p"$PW" --single-transaction --routines --triggers --events wordpress' \
    | docker exec -i -e PW="$DB_ROOT_PW" "$DB_CT" sh -c 'mariadb -uroot -p"$PW" wordpress'

  # Byte-copy proof, BEFORE the one deliberate change below: every table's
  # checksum on the clone equals the source's.
  local src_sums dst_sums
  src_sums="$(db_checksums "$SRC_DB" wordpress)"
  dst_sums="$(db_checksums "$DB_CT" wordpress)"
  if [[ -z "$src_sums" || "$src_sums" != "$dst_sums" ]]; then
    echo "clone FAILED — the copied database's table checksums differ from the source's:" >&2
    diff <(printf '%s\n' "$src_sums") <(printf '%s\n' "$dst_sums") >&2 || true
    exit 1
  fi
  local src_tree dst_tree
  src_tree="$(docroot_tree_sha1 "$SRC_WP")"
  dst_tree="$(docroot_tree_sha1 "$WP_CT")"
  if [[ "$src_tree" != "$dst_tree" ]]; then
    echo "clone FAILED — the copied docroot's file tree differs from the source's ($dst_tree vs $src_tree)" >&2
    exit 1
  fi

  # The one change: the source's address, everywhere it was written. Not
  # just home/siteurl — the importer writes resolved absolute URLs into
  # post_content (and plugins into serialized options), so a clone with
  # only the two options changed renders pages whose images and links still
  # point at the ORIGINAL, and a smoke test on it would quietly exercise
  # the wrong site. search-replace rewrites serialized values safely.
  echo "==> rewriting $SRC_URL -> $URL"
  docker exec "$WP_CT" wp --allow-root search-replace "$SRC_URL" "$URL" \
    --all-tables-with-prefix --precise --quiet
  local home
  home="$(docker exec "$WP_CT" wp --allow-root option get home)"
  if [[ "$home" != "$URL" ]]; then
    echo "clone FAILED — home is '$home' after the rewrite, expected '$URL'" >&2
    exit 1
  fi

  fix_wp_content_ownership "$WP_CT" clone
  docker exec "$WP_CT" wp --allow-root rewrite flush --hard
  assert_rewrite_rules "$WP_CT" clone

  jq -n \
    --arg slug "$DST" \
    --arg project "$PROJECT" \
    --arg wpContainer "$WP_CT" \
    --arg dbContainer "$DB_CT" \
    --arg network "$NETWORK" \
    --argjson port "$PORT" \
    --arg url "$URL" \
    --arg wpCli "docker exec $WP_CT wp --allow-root" \
    --arg createdAt "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --arg clonedFrom "$SRC" --arg clonedFromProject "$SRC_PROJECT" \
    '{slug:$slug, project:$project, wpContainer:$wpContainer, dbContainer:$dbContainer,
      network:$network, port:$port, url:$url, wpCli:$wpCli, createdAt:$createdAt,
      clonedFrom:$clonedFrom, clonedFromProject:$clonedFromProject, snapshot:null}' \
    > "$state"

  echo "clone ok — '$DST' is a copy of '$SRC' (db checksums and docroot tree verified before the URL rewrite)"
  echo "  url:          $URL"
  echo "  wp container: $WP_CT"
  echo "  wp-cli:       docker exec $WP_CT wp --allow-root"
  echo "  state file:   $state"
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
    # Exact `h2wp-<slug>-<6 hex>`, not a prefix: a clone is usually named
    # after its source (`foo` → `foo-b`), and `h2wp-foo-` alone also matches
    # `h2wp-foo-b-1a2b3c` — `down foo` would have torn down the clone too.
    local orphans
    orphans="$(projects_for_slug "$SLUG")"
    if [[ -z "$orphans" ]]; then
      echo "down — nothing to tear down for slug '$SLUG': no state file at $state and no running project matches h2wp-${SLUG}-<runid>"
      exit 0
    fi
    echo "down — no state file at $state, but docker still has project(s) for this slug:" >&2
    local n=0
    while IFS= read -r proj; do
      [[ -z "$proj" ]] && continue
      echo "  tearing down $proj" >&2
      if ! teardown_project "$proj"; then
        echo "down FAILED — containers of orphaned project '$proj' survived teardown" >&2
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
  reset) reset_cmd "$@" ;;
  clone) clone_cmd "$@" ;;
  down) down_cmd "$@" ;;
  *)
    echo "usage: test-env.sh <up|check|reset|clone|down> <slug> [args]" >&2
    exit 2
    ;;
esac

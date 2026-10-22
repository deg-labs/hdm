#!/usr/bin/env bash
set -euo pipefail

archive=${1:?application archive is required}
environment_file=${2:?environment file is required}
target_dir=${3:-/opt/apps/hdm}
mkdir -p "$target_dir/data"

# Stop the writer before copying SQLite, including WAL and pending-ID journals.
if [[ -f "$target_dir/docker-compose.yml" ]]; then
  container_id=$(docker compose --project-directory "$target_dir" ps -aq hdm)
  if [[ -n "$container_id" ]]; then
    docker stop "$container_id" >/dev/null
    mount_source=$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/app/data"}}{{.Source}}{{end}}{{end}}' "$container_id")
    if [[ "$mount_source" != "$target_dir/data" ]]; then
      migration_dir=$(mktemp -d)
      trap 'rm -rf "$migration_dir"' EXIT
      # Failure aborts deployment and leaves the old container available for recovery.
      docker cp "$container_id:/app/data/." "$migration_dir/"
      while IFS= read -r -d '' source_file; do
        relative=${source_file#"$migration_dir/"}
        destination="$target_dir/data/$relative"
        if [[ -f "$destination" ]] && ! cmp -s "$source_file" "$destination"; then
          echo "Conflicting persistent data: $relative; deployment aborted" >&2
          exit 1
        fi
      done < <(find "$migration_dir" -type f -print0)
      cp -a "$migration_dir/." "$target_dir/data/"
    fi
  fi
  docker compose --project-directory "$target_dir" down
fi

# Keep data across releases; unpacking code must not remove the ID ledger.
tar -xzf "$archive" -C "$target_dir"
install -m 600 "$environment_file" "$target_dir/.env"
cd "$target_dir"
docker compose up -d --build
docker image prune -f || true

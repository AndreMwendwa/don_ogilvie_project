#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
MANIM_BIN="${MANIM_BIN:-manim}"
YAML_FILE="${1:-}"
OUTPUT_DIR="${2:-${SCRIPT_DIR}/videos}"

if [[ -z "$YAML_FILE" || $# -gt 2 ]]; then
    printf 'Usage: %s YAML_FILE [OUTPUT_DIR]\n' "${BASH_SOURCE[0]}" >&2
    exit 2
fi

if [[ "$YAML_FILE" != /* ]]; then
    YAML_FILE="$(cd -- "$(dirname -- "$YAML_FILE")" && pwd)/$(basename -- "$YAML_FILE")"
fi
[[ -f "$YAML_FILE" ]] || { printf 'ERROR: YAML input not found: %s\n' "$YAML_FILE" >&2; exit 1; }
command -v "$MANIM_BIN" >/dev/null 2>&1 || {
    printf 'ERROR: Manim executable not found: %s\n' "$MANIM_BIN" >&2
    exit 1
}

mkdir -p "$OUTPUT_DIR"
export MANIM_YAML_FILE="$YAML_FILE"
"$MANIM_BIN" -qh --media_dir "$OUTPUT_DIR" \
    "${SCRIPT_DIR}/manim_multi_question_openai_mux_fixed.py" YAMLAnimation

#!/usr/bin/env bash
# Bring up the whole lab: reseed, start the scheduler API, start the UI.
#
#   ./demo.sh              quiet lab, break things by hand from the UI
#   ./demo.sh --chaos 0.3  random faults at 30% of operations
#   ./demo.sh --reset-only just reseed and exit
#   ./demo.sh --dev        Next dev server instead of a production build
#
# The UI is built and served in production mode by default. `next dev` puts its
# route indicator in the bottom-left corner, on top of the chaos bar and the
# error line: fine while iterating, wrong on camera.
set -euo pipefail

cd "$(dirname "$0")"
API_PORT="${API_PORT:-8791}"
UI_PORT="${UI_PORT:-3000}"
CHAOS=0
RESET_ONLY=0
DEV=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --chaos)      CHAOS="$2"; shift 2 ;;
    --reset-only) RESET_ONLY=1; shift ;;
    --dev)        DEV=1; shift ;;
    --api-port)   API_PORT="$2"; shift 2 ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done

PY=backend/.venv/bin/python
if [[ ! -x "$PY" ]]; then
  echo "==> creating backend venv"
  (cd backend && uv venv .venv && uv pip install -e ".[dev]")
fi

if ! psql -lqt 2>/dev/null | cut -d'|' -f1 | grep -qw labsched; then
  echo "==> creating database 'labsched'"
  createdb labsched
fi

echo "==> seeding lab (chaos=$CHAOS)"
(cd backend && .venv/bin/python -m labsched.seed --reset --chaos "$CHAOS")
[[ "$RESET_ONLY" == 1 ]] && exit 0

cleanup() { echo; echo "==> stopping"; kill 0 2>/dev/null || true; }
trap cleanup EXIT INT TERM

echo "==> scheduler + API on :$API_PORT"
(cd backend && .venv/bin/uvicorn labsched.api:app --port "$API_PORT" --log-level warning) &

until curl -sf "http://127.0.0.1:$API_PORT/api/health" >/dev/null 2>&1; do sleep 0.5; done
echo "    api ready"

[[ -d frontend/node_modules ]] || (cd frontend && bun install)

if [[ "$DEV" == 1 ]]; then
  echo "==> UI on :$UI_PORT (dev)"
  (cd frontend && LABSCHED_API="http://127.0.0.1:$API_PORT" bun run dev --port "$UI_PORT") &
else
  echo "==> building UI"
  (cd frontend && LABSCHED_API="http://127.0.0.1:$API_PORT" bun run build >/dev/null)
  echo "==> UI on :$UI_PORT"
  (cd frontend && LABSCHED_API="http://127.0.0.1:$API_PORT" bun run start --port "$UI_PORT") &
fi

cat <<EOF

  ---------------------------------------------------------------
  UI          http://localhost:$UI_PORT
  API         http://127.0.0.1:$API_PORT/docs

  Try, in this order:

  1. Workflows: two registered protocols, pinned by digest. Submit
     one and watch the floor pick it up.
  2. Factory floor: plates move, instruments light up as they run.
     "Submit run" (top right) is the form version, with admission
     checked before anything is spent.
  3. "Break it" -> plate_stuck. An instrument faults and holds both
     the deck and the plate; an alert appears. The picker only offers
     instruments that could physically raise the fault you chose.
  4. Interventions: the question, what the machine could NOT observe,
     the corroboration, and each option's computed blast radius.
  5. Switch "acting as" between the two identities. The operator can
     free a gripper and take an instrument out of service; only the
     client can discard their own plate or accept a suspect number.
  6. Audit: filter by token to trace every reservation to the
     lineage that authorised it.
  ---------------------------------------------------------------

EOF

wait

#!/usr/bin/env bash
# Install Ricardi as a systemd user service.
#
# A *user* unit rather than a system one, deliberately: it runs as you, with
# your ~/.claude, and with no more access than you have. A system unit would
# run as root with a different home, so the agents would start and then fail
# to authenticate -- and would be far more privileged than something that runs
# shell commands on request from a chat channel should be.
#
#   ./scripts/install-service.sh --dry-run
#   ./scripts/install-service.sh
#   ./scripts/install-service.sh --uninstall
set -euo pipefail

NAME=ricardi
DRY_RUN=0
UNINSTALL=0
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT="$HOME/.config/systemd/user/$NAME.service"

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)   DRY_RUN=1 ;;
    --uninstall) UNINSTALL=1 ;;
    --name)      NAME="$2"; UNIT="$HOME/.config/systemd/user/$NAME.service"; shift ;;
    -h|--help)   sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)           echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

run() { if [ "$DRY_RUN" = 1 ]; then echo "would run: $*"; else "$@"; fi; }

if [ "$UNINSTALL" = 1 ]; then
  run systemctl --user disable --now "$NAME" || true
  if [ "$DRY_RUN" = 1 ]; then echo "would remove: $UNIT"; else rm -f "$UNIT"; fi
  run systemctl --user daemon-reload
  echo
  echo "Removed $NAME. state/ is untouched, so bindings, sessions and the audit log survive."
  exit 0
fi

# --- what has to be true before a service is worth installing -----------
# Reported together: finding these one restart at a time is the slow way.
problems=()
command -v systemctl >/dev/null 2>&1 || problems+=("systemctl not found; this needs systemd.")
[ -x "$ROOT/.venv/bin/python" ] || problems+=("No virtualenv at $ROOT/.venv. Run: uv venv && uv pip install -e '.[slack,sdk]'")
for f in agents.toml policy.toml .env; do
  [ -f "$ROOT/$f" ] || problems+=("Missing $f. Copy the example beside it and fill it in.")
done
# Claude Code's credentials live in this user's home; without them the agents
# start and cannot authenticate.
[ -d "$HOME/.claude" ] || problems+=("No $HOME/.claude. Run \`claude\` once and sign in first.")

if [ ${#problems[@]} -gt 0 ]; then
  echo "Not ready to install:"
  printf '  - %s\n' "${problems[@]}"
  exit 1
fi

unit_text() {
  cat <<UNIT
[Unit]
Description=Ricardi: a Slack proxy to Claude Code agents
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$ROOT
ExecStart=$ROOT/.venv/bin/python -u -m ricardi.cli --transport slack --backend sdk
Restart=on-failure
RestartSec=10
# Socket Mode dials out, so nothing needs to listen. Keep it that way.
PrivateTmp=yes
NoNewPrivileges=yes

[Install]
WantedBy=default.target
UNIT
}

if [ "$DRY_RUN" = 1 ]; then
  echo "would write $UNIT:"
  unit_text | sed 's/^/    /'
  echo "would run: systemctl --user daemon-reload"
  echo "would run: systemctl --user enable --now $NAME"
  echo "would run: loginctl enable-linger $USER"
  exit 0
fi

mkdir -p "$(dirname "$UNIT")" "$ROOT/state"
unit_text > "$UNIT"
systemctl --user daemon-reload
systemctl --user enable --now "$NAME"

# Without lingering, a user unit stops when the last session logs out -- which
# is exactly what happens to a box you ssh into and then leave.
loginctl enable-linger "$USER" 2>/dev/null || \
  echo "note: could not enable lingering; the service will stop when you log out."

echo
systemctl --user --no-pager status "$NAME" | head -5
echo
echo "Watch:  journalctl --user -u $NAME -f"
echo
echo "Then prove a restart keeps a conversation: docs/running.md, \"Check it is"
echo "actually working\". A bot that answers but has forgotten the last hour looks fine."

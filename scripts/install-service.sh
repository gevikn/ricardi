#!/usr/bin/env bash
# Install Ricardi as a service that survives logging out.
#
#   Linux   a systemd *user* unit, plus lingering so it does not stop when you
#           log out of the box you sshed into. No root except for lingering.
#   macOS   a launchd LaunchDaemon. Root to install, because anything that runs
#           with nobody logged in lives in /Library; it drops to your account
#           to run, because an agent that executes shell commands on request
#           from a chat channel has no business being root.
#
# Admin to install, unprivileged to run. If a deployment inverts that, it is
# the wrong deployment.
#
#   ./scripts/install-service.sh --dry-run
#   ./scripts/install-service.sh                 # Linux
#   sudo ./scripts/install-service.sh            # macOS
#   ./scripts/install-service.sh --uninstall
set -euo pipefail

NAME=ricardi
DRY_RUN=0
UNINSTALL=0
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OS="$(uname -s)"

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)   DRY_RUN=1 ;;
    --uninstall) UNINSTALL=1 ;;
    --name)      NAME="$2"; shift ;;
    -h|--help)   sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)           echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

UNIT="$HOME/.config/systemd/user/$NAME.service"
LABEL="local.$NAME"
PLIST="/Library/LaunchDaemons/$LABEL.plist"

# sudo keeps the real user here, which is who the daemon should run as -- the
# whole point is not to run as root.
RUN_USER="${SUDO_USER:-${USER:-$(id -un)}}"

run() { if [ "$DRY_RUN" = 1 ]; then echo "would run: $*"; else "$@"; fi; }

home_of() {
  local h=""
  if [ "$OS" = Darwin ]; then
    h="$(dscl . -read "/Users/$1" NFSHomeDirectory 2>/dev/null | awk '{print $2}')"
    [ -n "$h" ] || h="/Users/$1"
  else
    h="$(getent passwd "$1" 2>/dev/null | cut -d: -f6)"
    [ -n "$h" ] || h="$HOME"
  fi
  printf '%s\n' "$h"
}
RUN_HOME="$(home_of "$RUN_USER")"

# A credential belongs in .env and nowhere else, so "is there one" is a grep.
have_env_key() {
  [ -f "$ROOT/.env" ] && grep -Eq "^[[:space:]]*$1=.+" "$ROOT/.env"
}
have_oauth_token() { have_env_key CLAUDE_CODE_OAUTH_TOKEN; }
have_api_key()     { have_env_key ANTHROPIC_API_KEY; }
have_credential()  { have_oauth_token || have_api_key; }

# Exactly one credential, or the wrong one wins in silence. A stored login
# takes precedence over the environment, so a signed-in home on an always-on
# box shadows whatever is in .env -- and Ricardi then dies whenever that login
# stops refreshing, weeks later, with a perfectly good token two feet away.
STORED_LOGIN="$RUN_HOME/.claude/.credentials.json"
check_one_credential() {
  if have_credential && [ -f "$STORED_LOGIN" ]; then
    problems+=("Two credentials: one in .env and a stored login at $STORED_LOGIN. The stored login takes precedence, so the one in .env would be ignored until that login stops refreshing. Keep one -- remove $STORED_LOGIN, or remove the credential from .env.")
  fi
  if have_api_key && have_oauth_token; then
    problems+=("Both ANTHROPIC_API_KEY and CLAUDE_CODE_OAUTH_TOKEN are set in .env. They conflict; keep exactly one.")
  fi
}

# --- preflight ----------------------------------------------------------
# Reported together: finding these one restart at a time is the slow way, and
# a service that dies at 3am because a file was missing is a poor trade for
# the thirty lines it takes to say so now.
problems=()

check_common() {
  # Only `sudo` exports SUDO_USER. From a root shell -- `sudo -i`, `su -`, a
  # provisioning script, CI -- RUN_USER falls through to USER or `id -un` and
  # becomes root. The macOS branch's own root check passes, because being root
  # is exactly what it asks for, so the plist would say UserName=root and
  # launchd would run an agent that executes shell commands from a chat channel
  # as root. Refusing beats guessing who was meant to own it.
  if [ "$RUN_USER" = root ]; then
    problems+=("This would install the service to run as root, which is the one thing it must not do. Run it from the shell of the user that should own the agent: 'sudo ./scripts/install-service.sh'. A root shell leaves SUDO_USER unset, so there is nobody else to run as.")
  fi
  # Quoted: zsh is the default shell on macOS and globs the brackets otherwise.
  [ -x "$ROOT/.venv/bin/python" ] || problems+=("No virtualenv at $ROOT/.venv. Run: uv venv && uv pip install -e '.[slack,sdk]'")
  local f
  for f in agents.toml policy.toml .env; do
    [ -f "$ROOT/$f" ] || problems+=("Missing $f. Copy the example beside it and fill it in.")
  done
  check_one_credential
}

if [ "$OS" = Linux ]; then
  command -v systemctl >/dev/null 2>&1 || problems+=("systemctl not found; this needs systemd.")
  check_common
  # A user unit keeps your home, so Claude Code's own credentials are reachable
  # and an explicit credential is optional here -- the one platform where
  # signing in interactively is a legitimate answer.
  if [ ! -d "$HOME/.claude" ] && ! have_credential; then
    problems+=("No $HOME/.claude and no credential in .env. Either sign in once with 'claude', or put ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN in .env -- see docs/running.md.")
  fi

elif [ "$OS" = Darwin ]; then
  check_common
  if [ "$UNINSTALL" = 0 ] && [ "$DRY_RUN" = 0 ] && [ "$(id -u)" != 0 ]; then
    problems+=("Installing a LaunchDaemon needs root: sudo $0")
  fi
  # The one that actually bites. On macOS Claude Code keeps its OAuth token in
  # the login Keychain, and a LaunchDaemon runs outside any login session, so
  # the Keychain is locked and unreachable. ~/.claude existing proves nothing.
  # An API key, or a token from 'claude setup-token', is the way in: the SDK
  # hands its whole environment to the CLI it spawns, so .env is enough.
  if ! have_credential; then
    problems+=("macOS needs a credential in .env -- a LaunchDaemon cannot reach your login Keychain, which is where Claude Code keeps credentials on this platform. Use ANTHROPIC_API_KEY from the Anthropic Console, or run 'claude setup-token' (needs a browser once); put it in .env and chmod 600 .env. Not in the plist: /Library is world-readable.")
  fi
  # A LaunchDaemon starts with a bare PATH, so the CLI the SDK spawns has to be
  # findable from one of the directories the plist names below.
  CLAUDE_BIN=""
  for cand in "$RUN_HOME/.local/bin/claude" /usr/local/bin/claude /opt/homebrew/bin/claude; do
    if [ -x "$cand" ]; then CLAUDE_BIN="$cand"; break; fi
  done
  [ -n "$CLAUDE_BIN" ] || problems+=("No claude binary in ~/.local/bin, /usr/local/bin or /opt/homebrew/bin. A LaunchDaemon does not inherit your PATH, so it has to be in one of those. Install: curl -fsSL https://claude.ai/install.sh | bash")

else
  echo "Unsupported: $OS. Linux uses systemd, macOS uses launchd." >&2
  echo "On Windows use scripts\\install-service.ps1, and read docs/running.md first." >&2
  exit 2
fi

if [ "$UNINSTALL" = 0 ] && [ ${#problems[@]} -gt 0 ]; then
  echo "Not ready to install:"
  printf '  - %s\n' "${problems[@]}"
  exit 1
fi

# --- Linux: systemd user unit -------------------------------------------
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

install_systemd() {
  if [ "$DRY_RUN" = 1 ]; then
    echo "would write $UNIT:"
    unit_text | sed 's/^/    /'
    echo "would run: systemctl --user daemon-reload"
    echo "would run: systemctl --user enable --now $NAME"
    echo "would run: loginctl enable-linger $USER"
    return
  fi

  mkdir -p "$(dirname "$UNIT")" "$ROOT/state"
  unit_text > "$UNIT"
  systemctl --user daemon-reload
  systemctl --user enable --now "$NAME"

  # Without lingering, a user unit stops when the last session logs out --
  # which is exactly what happens to a box you ssh into and then leave.
  loginctl enable-linger "$USER" 2>/dev/null || \
    echo "note: could not enable lingering; the service will stop when you log out."

  echo
  systemctl --user --no-pager status "$NAME" | head -5
  echo
  echo "Watch:  journalctl --user -u $NAME -f"
}

remove_systemd() {
  run systemctl --user disable --now "$NAME" || true
  if [ "$DRY_RUN" = 1 ]; then echo "would remove: $UNIT"; else rm -f "$UNIT"; fi
  run systemctl --user daemon-reload
}

# --- macOS: launchd LaunchDaemon ----------------------------------------
plist_text() {
  # launchd captures stderr for us, which is worth having for a traceback from
  # before logging exists -- a bad venv, a missing package. The main log goes
  # through --log-file instead, because that one rotates and StandardErrorPath
  # grows until the disk is full.
  cat <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>              <string>$LABEL</string>
  <key>WorkingDirectory</key>   <string>$ROOT</string>

  <!-- Not root. The install needs it; the service does not. -->
  <key>UserName</key>           <string>$RUN_USER</string>
  <key>GroupName</key>          <string>staff</string>

  <key>ProgramArguments</key>
  <array>
    <string>$ROOT/.venv/bin/python</string>
    <string>-u</string>
    <string>-m</string>
    <string>ricardi.cli</string>
    <string>--transport</string><string>slack</string>
    <string>--backend</string><string>sdk</string>
    <string>--log-file</string><string>$ROOT/state/service.log</string>
  </array>

  <key>RunAtLoad</key>          <true/>
  <key>KeepAlive</key>          <true/>
  <!-- Ten seconds between respawns, so a crash loop does not spin the CPU. -->
  <key>ThrottleInterval</key>   <integer>10</integer>

  <key>StandardErrorPath</key>  <string>$ROOT/state/launchd-stderr.log</string>

  <!-- A LaunchDaemon starts with a bare PATH, so the claude CLI that the SDK
       spawns would not be found. No token here: /Library is world-readable,
       and Ricardi reads .env from WorkingDirectory itself. -->
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>$RUN_HOME/.local/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
</dict>
</plist>
PLIST
}

install_launchd() {
  if [ "$DRY_RUN" = 1 ]; then
    echo "would write $PLIST (root:wheel, 0644):"
    plist_text | sed 's/^/    /'
    echo "would run: launchctl bootstrap system $PLIST"
    echo
    echo "It would run as $RUN_USER, not root."
    return
  fi

  mkdir -p "$ROOT/state"
  chown "$RUN_USER" "$ROOT/state" 2>/dev/null || true
  plist_text > "$PLIST"
  # launchd silently refuses a plist that is group- or world-writable, or one
  # not owned by root. The failure looks like the service never starting.
  chown root:wheel "$PLIST"
  chmod 644 "$PLIST"

  # bootstrap is the modern verb; load -w is kept for older systems.
  launchctl bootstrap system "$PLIST" 2>/dev/null || launchctl load -w "$PLIST"

  echo
  launchctl print "system/$LABEL" 2>/dev/null | head -8 || true
  echo
  echo "Runs as $RUN_USER, not root."
  echo "Watch:  tail -f $ROOT/state/service.log"
  echo "Crashes before logging starts: $ROOT/state/launchd-stderr.log"
}

remove_launchd() {
  if [ "$DRY_RUN" = 1 ]; then
    echo "would run: launchctl bootout system/$LABEL"
    echo "would remove: $PLIST"
    return
  fi
  if [ "$(id -u)" != 0 ]; then
    echo "Removing a LaunchDaemon needs root: sudo $0 --uninstall" >&2
    exit 1
  fi
  launchctl bootout "system/$LABEL" 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
}

# --- go -----------------------------------------------------------------
if [ "$UNINSTALL" = 1 ]; then
  if [ "$OS" = Darwin ]; then remove_launchd; else remove_systemd; fi
  echo
  echo "Removed $NAME. state/ is untouched, so bindings, sessions and the audit log survive."
  exit 0
fi

if [ "$OS" = Darwin ]; then install_launchd; else install_systemd; fi

echo
echo "Then prove a restart keeps a conversation: docs/running.md, 'Check it is"
echo "actually working'. A bot that answers but has forgotten the last hour looks fine."

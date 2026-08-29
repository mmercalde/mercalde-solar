#!/bin/bash
# install.sh - set the solar agent up on the KAMRUI.
#
#   git clone https://github.com/mmercalde/mercalde-solar.git
#   cd mercalde-solar && git checkout agent
#   ./agent/install.sh
#
# Idempotent: safe to re-run after a git pull.

set -euo pipefail

AGENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${AGENT_DIR}/venv"
SERVICE_SRC="${AGENT_DIR}/solar-agent.service"
SERVICE_DST="/etc/systemd/system/solar-agent.service"

say() { printf '\n==> %s\n' "$*"; }

say "Agent directory: ${AGENT_DIR}"

if [ "${AGENT_DIR}" != "/home/michael/mercalde-solar/agent" ]; then
    echo "WARNING: solar-agent.service expects /home/michael/mercalde-solar/agent."
    echo "         Edit WorkingDirectory and ExecStart in ${SERVICE_SRC} to match."
fi

say "Python"
python3 --version
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "ERROR: Python 3.10 or newer is required."
    exit 1
fi

say "Virtualenv"
if [ ! -d "${VENV}" ]; then
    python3 -m venv "${VENV}"
    echo "created ${VENV}"
else
    echo "${VENV} already exists"
fi
"${VENV}/bin/pip" install --quiet --upgrade pip
"${VENV}/bin/pip" install --quiet -r "${AGENT_DIR}/requirements.txt"
echo "dependencies installed"

say "Data directory"
mkdir -p "${AGENT_DIR}/data"
echo "${AGENT_DIR}/data ready"

say "Config"
if [ ! -f "${AGENT_DIR}/config.json" ]; then
    cp "${AGENT_DIR}/config.example.json" "${AGENT_DIR}/config.json"
    chmod 600 "${AGENT_DIR}/config.json"
    cat <<'MSG'
Created agent/config.json from the example. Fill in before starting:

  telegram.token / telegram.chat_id   the same bot the dashboard uses; both
                                      are visible in the dashboard's own
                                      config.json, or at GET /config
  gateway.password                    the InsightLocal Admin password, needed
                                      only for the history backfill

config.json is gitignored and mode 600.
MSG
else
    echo "agent/config.json already exists; leaving it alone"
fi

say "Checking the local model"
if "${VENV}/bin/python" "${AGENT_DIR}/llm_probe.py"; then
    echo "llama-server is answering"
else
    echo "WARNING: the LLM probe failed. Check 'systemctl status llama-server'."
fi

say "Tests"
( cd "${AGENT_DIR}" && "${VENV}/bin/python" -E -m pytest -q )

say "systemd unit"
if [ "$(id -u)" -eq 0 ]; then
    install -m 644 "${SERVICE_SRC}" "${SERVICE_DST}"
    systemctl daemon-reload
    echo "installed ${SERVICE_DST}. Enable it when you are ready:"
    echo "    systemctl enable --now solar-agent"
else
    cat <<MSG
Not running as root, so the unit was not installed. Run:

    sudo install -m 644 ${SERVICE_SRC} ${SERVICE_DST}
    sudo systemctl daemon-reload
    sudo systemctl enable --now solar-agent
MSG
fi

cat <<MSG

==> Next

  1. Fill in agent/config.json (Telegram, and gateway.password for backfill).
  2. Discover the gateway API and backfill history:
         ${VENV}/bin/python ${AGENT_DIR}/scrape_gateway.py --discover
         ${VENV}/bin/python ${AGENT_DIR}/scrape_gateway.py --backfill
  3. Try one tick without writing anything:
         ${VENV}/bin/python ${AGENT_DIR}/agent.py --dry-run
  4. Start it:
         sudo systemctl enable --now solar-agent
         journalctl -u solar-agent -f

  The guard refuses every write until the learning gate opens. Check it with:
         ${VENV}/bin/python -c "import sys; sys.path.insert(0,'${AGENT_DIR}'); \\
             import config, history, loadmodel, json; c=config.load(); \\
             print(json.dumps(loadmodel.LoadModel(history.connect(), c).learning_status(), indent=1))"
MSG

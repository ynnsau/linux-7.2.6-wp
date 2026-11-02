#!/bin/sh
# Install once; start/stop/status a frozen, resumable Redis latency campaign.
set -eu
ROOT=/home/yans3/cmdp-v2/redis-ycsb-study/tail-latency-v1
UNIT=cmdp-redis-tail-v1.service
ACTION=${1:-status}
SCRIPTS=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

case "$ACTION" in
  status)
    systemctl --no-pager --full status "$UNIT" || true
    if [ -f "$ROOT/source/scripts/redis-ycsb-tail-campaign.py" ]; then
      python3 "$ROOT/source/scripts/redis-ycsb-tail-campaign.py" status
    fi
    ;;
  install)
    [ "$(id -u)" = 0 ] || { echo 'Run install with sudo.' >&2; exit 1; }
    [ ! -e "$ROOT" ] || { echo 'Evidence root exists; use status/start, never overwrite it.' >&2; exit 1; }
    [ ! -e "/etc/systemd/system/$UNIT" ] || { echo 'Unit already exists; inspect it before reinstalling.' >&2; exit 1; }
    mkdir -p "$ROOT/source/scripts" "$ROOT/bin"
    cp -p "$SCRIPTS/"*.py "$SCRIPTS/"*.md "$SCRIPTS/redis-ycsb-tail-service.sh" "$ROOT/source/scripts/"
    cp -p "$SCRIPTS/../cmdp_exec" "$ROOT/source/cmdp_exec"
    python3 "$ROOT/source/scripts/redis-ycsb-tail-campaign.py" prepare
    cat > "$ROOT/bin/run-tail-campaign" <<'EOF'
#!/bin/sh
set -eu
ROOT=/home/yans3/cmdp-v2/redis-ycsb-study/tail-latency-v1
exec /usr/bin/python3 -u "$ROOT/source/scripts/redis-ycsb-tail-campaign.py" run >> "$ROOT/launch.log" 2>&1
EOF
    chmod 755 "$ROOT/bin/run-tail-campaign"
    cat > "/etc/systemd/system/$UNIT" <<'EOF'
[Unit]
Description=CMD-P Redis YCSB matched tail latency campaign v1
After=local-fs.target

[Service]
Type=simple
User=root
WorkingDirectory=/home/yans3/cmdp-v2/redis-ycsb-study/tail-latency-v1
ExecStart=/home/yans3/cmdp-v2/redis-ycsb-study/tail-latency-v1/bin/run-tail-campaign
KillMode=mixed
KillSignal=SIGTERM
TimeoutStopSec=600
Restart=no
CPUAffinity=24
UMask=0022
EOF
    systemd-analyze verify "/etc/systemd/system/$UNIT"
    systemctl daemon-reload
    echo "Installed $UNIT; not started or enabled at boot."
    ;;
  start|resume)
    [ "$(id -u)" = 0 ] || { echo 'Run start with sudo.' >&2; exit 1; }
    systemctl start "$UNIT"
    ;;
  stop)
    [ "$(id -u)" = 0 ] || { echo 'Run stop with sudo.' >&2; exit 1; }
    systemctl stop "$UNIT"
    ;;
  *) echo 'Usage: redis-ycsb-tail-service.sh {install|start|resume|status|stop}' >&2; exit 2 ;;
esac

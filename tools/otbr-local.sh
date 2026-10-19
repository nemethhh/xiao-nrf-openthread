#!/usr/bin/env bash
# Run the OpenThread Border Router (ot-br-posix) in Docker against the XIAO RCP, on an isolated network.
#
#   tools/otbr-local.sh up <baud>      start the container (115200 or 460800)
#   tools/otbr-local.sh down           stop and remove the container (the network stays; `net-rm` removes it)
#   tools/otbr-local.sh ctl <args...>  run ot-ctl inside the container
#   tools/otbr-local.sh logs           print the container log
#   tools/otbr-local.sh net-rm         remove the isolated network
#
# The container sits on `otbr-isolated`, a Docker --internal network: it has no route to the host LAN
# or the internet, so a test Thread network and its border router cannot leak onto the home network.
set -euo pipefail

# Pinned by tag and digest. This is the ot-br-posix commit (fd872ab) that Home Assistant's
# openthread_border_router add-on 3.3.0 builds as its stable OTBR (docs/reference/ha-addons/.../build.yaml).
IMAGE="openthread/border-router:sha-fd872ab@sha256:9b1bb839de0853c59b2c4cefa2d64b97dd98ec04a5572955f395971df2486151"
NAME="otbr-local"
NETWORK="otbr-isolated"
SUBNET="fd0b:0b0b:0b0b::/64"
RCP_BY_ID="/dev/serial/by-id/usb-Seeed_Studio_Seeed_Studio_XIAO_nrf54_CMSIS-DAP_AA2C939D-if02"

# Radio URL query, selected by OTBR_FLOW. The two HA add-on forms are:
#   hw       "flow_control: true"  -> ?uart-baudrate=<baud>&uart-flow-control        (default here)
#   deassert "flow_control: false" -> ?uart-baudrate=<baud>&uart-init-deassert
# plus `none` (?uart-baudrate=<baud> only), which the add-on cannot produce.
# With the XIAO's SAMD11 bridge `deassert` does NOT work: it drops DTR, and the bridge then stops forwarding
# in both directions (docs/hardware-notes.md), so otbr-agent fails with "spinel_driver.cpp Init() Failure".
# `hw` sets CRTSCTS on the host tty. With this bridge (no RTS/CTS wires) that was observed harmless on the dev host
# and on HA OS (the add-on's `flow_control: true` works there); the RCP firmware itself has no flow control (apps/rcp overlay).
OTBR_FLOW="${OTBR_FLOW:-hw}"
radio_url() {
    local q="uart-baudrate=$1"
    case "$OTBR_FLOW" in
    hw) q="$q&uart-flow-control" ;;
    deassert) q="$q&uart-init-deassert" ;;
    none) ;;
    *) echo "OTBR_FLOW must be hw, deassert or none" >&2; exit 2 ;;
    esac
    echo "spinel+hdlc+uart:///dev/ttyRCP?$q"
}

ensure_network() {
    if ! docker network inspect "$NETWORK" >/dev/null 2>&1; then
        docker network create --internal --ipv6 --subnet "$SUBNET" "$NETWORK" >/dev/null
    fi
    if [ "$(docker network inspect "$NETWORK" -f '{{.Internal}}')" != "true" ]; then
        echo "network $NETWORK is not --internal; refusing to start" >&2
        exit 1
    fi
}

cmd="${1:-}"
[ $# -gt 0 ] && shift
case "$cmd" in
up)
    baud="${1:?usage: otbr-local.sh up <baud>}"
    case "$baud" in 115200 | 460800) ;; *) echo "unsupported baud $baud" >&2; exit 2 ;; esac
    [ -e "$RCP_BY_ID" ] || { echo "RCP serial port $RCP_BY_ID not found" >&2; exit 1; }
    ensure_network
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    docker run -d --name "$NAME" --network "$NETWORK" --privileged --restart=no \
        --device "$(readlink -f "$RCP_BY_ID"):/dev/ttyRCP" \
        --sysctl net.ipv6.conf.all.disable_ipv6=0 --sysctl net.ipv6.conf.all.forwarding=1 \
        -e OT_RCP_DEVICE="$(radio_url "$baud")" -e OT_INFRA_IF=eth0 -e OT_THREAD_IF=wpan0 -e OT_LOG_LEVEL=7 \
        "$IMAGE" >/dev/null
    # Wait until otbr-agent answers on its control socket.
    for _ in $(seq 60); do
        if docker exec "$NAME" ot-ctl state >/dev/null 2>&1; then exit 0; fi
        sleep 1
    done
    echo "otbr-agent did not come up within 60 s" >&2
    docker logs "$NAME" 2>&1 | tail -30 >&2
    exit 1
    ;;
down)
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    ;;
ctl)
    docker exec "$NAME" ot-ctl "$@"
    ;;
logs)
    docker logs "$NAME" 2>&1
    ;;
net-rm)
    docker network rm "$NETWORK"
    ;;
*)
    sed -n '2,10p' "$0" >&2
    exit 2
    ;;
esac

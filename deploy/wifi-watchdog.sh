#!/usr/bin/env bash
# Wi-Fi watchdog: when the server can't reach its own router, reconnect the Wi-Fi,
# and reboot as a last resort.
#
# It pings the router, not the internet.
#
# Wi-Fi only: if the server moves to Ethernet, disable this.
#
# Try it without changing anything (no root needed). A gateway that isn't there makes
# every check fail, and DRY_RUN logs the actions instead of taking them:
#   DRY_RUN=1 CHECK_EVERY_S=1 STATE_DIR=/tmp/wifi-watchdog GATEWAY=192.0.2.1 deploy/wifi-watchdog.sh

set -u

iface="${IFACE:-wlan0}"
check_every_s="${CHECK_EVERY_S:-60}"

# Failed checks in a row: reconnect at every reconnect_every, reboot from reboot_after.
# At one check a minute: reconnect at 5 and 10 min, reboot at 15
reconnect_every=5
reboot_after=15

# At most one watchdog reboot per this many hours, so a router that's switched off
# doesn't reboot the server every 15 minutes; it keeps reconnecting instead
min_reboot_gap_h=6

# The journal lives in RAM here and is lost at reboot, so actions also go to a file
state_dir="${STATE_DIR:-/var/lib/wifi-watchdog}"
log_file="$state_dir/events.log"
max_log_kb=1024

log() {
    echo "$*"
    echo "$(date -Iseconds) $*" >> "$log_file"

    # Keep the newer half once the file outgrows max_log_kb
    if (( $(stat -c %s "$log_file") > max_log_kb * 1024 )); then
        tail -c $(( max_log_kb * 512 )) "$log_file" > "$log_file.tmp" && mv "$log_file.tmp" "$log_file"
    fi
}

run() {
    if [[ -n "${DRY_RUN:-}" ]]; then
        log "dry run: $*"
        return
    fi
    local out
    if out="$("$@" 2>&1)"; then
        log "done: $*"
    else
        log "failed: $*: $out"
    fi
}

gateway() {
    if [[ -n "${GATEWAY:-}" ]]; then
        echo "$GATEWAY"
        return
    fi
    ip -4 route show default dev "$iface" 2>/dev/null | awk '$2 == "via" { print $3; exit }'
}

# Any of 3 pings answered counts. No address or no default route (Oct 20) fails too
router_answers() {
    local gw
    gw="$(gateway)"
    [[ -n "$gw" ]] && ping -q -c 3 -W 2 -I "$iface" "$gw" > /dev/null 2>&1
}

# What the Wi-Fi looked like when it failed, for working out why afterwards
snapshot() {
    {
        echo "--- $iface"
        ip addr show "$iface"
        ip route
        iw dev "$iface" link
        echo "--- recent NetworkManager / wpa_supplicant"
        journalctl --no-pager -o short-iso -n 40 -u NetworkManager -u wpa_supplicant
        echo "--- recent Wi-Fi driver"
        journalctl --no-pager -o short-iso -k -n 500 | grep -E "brcmf|$iface|ieee80211" | tail -n 20
        echo "---"
    } >> "$log_file" 2>&1
}

reboot_allowed() {
    local last
    last="$(cat "$state_dir/last-reboot" 2>/dev/null || echo 0)"
    (( $(date +%s) - last >= min_reboot_gap_h * 3600 ))
}

mkdir -p "$state_dir"
echo "Watching $iface: pinging its router every ${check_every_s}s"

failures=0
while true; do
    if router_answers; then
        # A blip too short to act on stays out of the file
        if (( failures >= reconnect_every )); then
            log "router answers again after $failures failed checks"
        elif (( failures > 0 )); then
            echo "router answers again after $failures failed checks"
        fi
        failures=0
    else
        failures=$(( failures + 1 ))
        if (( failures == 1 )); then
            echo "no answer from router (gateway '$(gateway)'), retrying every ${check_every_s}s"
        fi

        if (( failures % reconnect_every == 0 )); then
            if (( failures == reconnect_every )); then
                log "no answer from router for $failures checks; state below"
                snapshot
            fi

            if (( failures >= reboot_after )) && reboot_allowed; then
                log "no answer from router for $failures checks: rebooting"
                date +%s > "$state_dir/last-reboot"
                run systemctl reboot
            else
                if (( failures >= reboot_after )); then
                    log "reboot skipped: the last watchdog reboot was under ${min_reboot_gap_h} h ago"
                fi
                log "no answer from router for $failures checks: reconnecting $iface"
                run nmcli device disconnect "$iface"
                run nmcli --wait 30 device connect "$iface"
            fi
        fi
    fi
    sleep "$check_every_s"
done

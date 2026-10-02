#!/bin/sh
# The only runtime writer of SleepDisabled. All state files are data, never shell code.
# argv: machine-wide directory, generation, pass interval, stale seconds, command timeout.
set -u
umask 077
state=$1
generation=$2
interval=$3
stale=$4
command_timeout=$5
sudo=/usr/bin/sudo
pmset=/usr/bin/pmset
ioreg=/usr/sbin/ioreg
ps=/bin/ps
date=/bin/date
# These overrides are exclusively for the fake-executable integration tests.
if [ "${RCP_POWER_TEST:-}" = 1 ]; then
    sudo=${RCP_POWER_SUDO:?}
    pmset=${RCP_POWER_PMSET:?}
    ioreg=${RCP_POWER_IOREG:?}
    ps=${RCP_POWER_PS:?}
    date=${RCP_POWER_DATE:-/bin/date}
fi
case "$generation" in ''|*[!0-9]*) exit 2 ;; esac

bounded() {
    "$@" &
    child=$!
    (
        trap 'kill "$sleeper" 2>/dev/null; exit 0' TERM INT
        sleep "$command_timeout" &
        sleeper=$!
        wait "$sleeper"
        # sudo forwards TERM to pmset; allow that cleanup before forcing the wrapper down.
        kill -TERM "$child" 2>/dev/null
        sleep 1 &
        sleeper=$!
        wait "$sleeper"
        kill -KILL "$child" 2>/dev/null
    ) &
    timer=$!
    wait "$child"
    result=$?
    kill "$timer" 2>/dev/null
    wait "$timer" 2>/dev/null
    return "$result"
}

field() {
    [ -f "$state/$1" ] || return 1
    while IFS='=' read -r key value; do
        if [ "$key" = "$2" ]; then
            printf '%s\n' "$value"
            return 0
        fi
    done < "$state/$1"
    return 1
}

atomic() {
    target=$1
    shift
    printf '%s\n' "$@" > "$state/.$target.$$" &&
        bounded /bin/mv -f "$state/.$target.$$" "$state/$target"
}

read_flag() {
    flag=$(bounded "$pmset" -g) || return 1
    while read -r key value rest; do
        case "$key:$value" in
            SleepDisabled:0|SleepDisabled:1|disablesleep:0|disablesleep:1)
                printf '%s\n' "$value"; return 0 ;;
        esac
    done <<FLAGS
$flag
FLAGS
    # pmset omits the line until the flag has been set once since boot.
    case "$flag" in *'System-wide power settings:'*) printf '0\n'; return 0 ;; esac
    return 1
}

flag_is() {
    actual_flag=$(read_flag) || return 1
    [ "$actual_flag" = "$1" ]
}

lid_is_open() {
    lid=$(bounded "$ioreg" -r -k AppleClamshellState) || return 1
    # Open only when every reported row says No, as parse_lid requires.
    open=1
    while read -r line; do
        # ioreg prefixes properties with tree glyphs: `  |   "AppleClamshellState" = No`.
        case "$line" in *'"AppleClamshellState"'*) ;; *) continue ;; esac
        prefix=${line%%'"AppleClamshellState"'*}
        case "$prefix" in *[!' |']*) continue ;; esac
        if [ "${line#"$prefix"}" = '"AppleClamshellState" = No' ]; then
            [ "$open" = 1 ] && open=0
        else
            open=2
        fi
    done <<LID
$lid
LID
    [ "$open" = 0 ]
}

release() {
    release_cause=$1
    now=$(bounded "$date" +%s) || now=0
    clear_failed=0
    sleep_failed=0
    # Save the cause first, best effort; a storage failure must not prevent cleanup.
    atomic result "generation=$generation" "cause=$release_cause" "at=$now" \
        'clear_failed=0' 'sleep_failed=0' 'complete=0'
    # Never clear somebody else's flag, including before our first activation.
    if [ "$(field activation generation)" = "$generation" ] &&
        [ "$(field activation set)" = 1 ]; then
        if ! bounded "$sudo" -n "$pmset" -a disablesleep 0 || ! flag_is 0; then
            clear_failed=1
        else
            atomic activation "generation=$generation" "pid=$worker_pid" \
                "start=$worker_start" 'set=0'
        fi
        if ! lid_is_open; then
            bounded "$pmset" sleepnow || sleep_failed=1
        fi
    fi
    atomic result "generation=$generation" "cause=$release_cause" "at=$now" \
        "clear_failed=$clear_failed" "sleep_failed=$sleep_failed" 'complete=1'
}

worker_pid=
worker_start=
revoked=$(field revoked generation) || revoked=0
case "$revoked" in ''|*[!0-9]*) exit 2 ;; esac
[ "$generation" -gt "$revoked" ] || exit 0
atomic ack "generation=$generation" "watchdog_pid=$$" || exit 1
# The backend cannot request on before this acknowledgment exists.
sleep "$interval"
while :; do
    # Snapshot the atomic heartbeat once; never mix two generations of fields.
    heartbeat=$(bounded /bin/cat "$state/heartbeat" 2>/dev/null) || heartbeat=
    heartbeat_generation= heartbeat_pid= heartbeat_start= desired= cause= at=
    while IFS='=' read -r key value; do
        case "$key" in
            generation) heartbeat_generation=$value ;;
            pid) heartbeat_pid=$value ;;
            start) heartbeat_start=$value ;;
            desired) desired=$value ;;
            cause) cause=$value ;;
            at) at=$value ;;
        esac
    done <<HEARTBEAT
$heartbeat
HEARTBEAT
    worker_pid=$heartbeat_pid
    worker_start=$heartbeat_start
    valid=true
    case "$at" in ''|*[!0-9]*) valid=false ;; esac
    case "$worker_pid" in ''|*[!0-9]*) valid=false ;; esac
    now=$(bounded "$date" +%s) || valid=false
    if [ "$valid" = true ]; then
        [ "$heartbeat_generation" = "$generation" ] &&
            [ "$now" -ge "$at" ] && [ "$((now - at))" -le "$stale" ] || valid=false
    fi
    if [ "$valid" = true ]; then
        actual_start=$(bounded "$ps" -p "$worker_pid" -o lstart=) || actual_start=
        actual_start=${actual_start#"${actual_start%%[![:space:]]*}"}
        actual_start=${actual_start%"${actual_start##*[![:space:]]}"}
        [ -n "$actual_start" ] && [ "$actual_start" = "$worker_start" ] || valid=false
    fi
    if [ "$valid" != true ]; then
        # Persist revocation before cleanup so a resumed writer cannot re-arm this generation.
        atomic revoked "generation=$generation"
        release heartbeat_stale
        exit 0
    fi
    if [ "$desired" != on ]; then
        case "$cause" in
            demand_gone|battery_floor|thermal|reading_failed|watchdog_lost|heartbeat_stale|disabled|shutdown) ;;
            *) cause=reading_failed ;;
        esac
        release "$cause"
        exit 0
    fi
    if [ "$(field activation generation)" != "$generation" ] ||
        [ "$(field activation set)" != 1 ]; then
        # Refuse an external flag even if it changed after the backend's safety pass.
        if ! flag_is 0; then
            release reading_failed
            exit 1
        fi
        # Ownership is recorded before attempting the write, including an interrupted write.
        atomic activation "generation=$generation" "pid=$worker_pid" \
            "start=$worker_start" 'set=1' || exit 1
        if ! bounded "$sudo" -n "$pmset" -a disablesleep 1 || ! flag_is 1; then
            release reading_failed
            exit 1
        fi
    else
        current_flag=$(read_flag) || {
            release reading_failed
            exit 1
        }
        if [ "$current_flag" = 0 ]; then
            if ! bounded "$sudo" -n "$pmset" -a disablesleep 1 || ! flag_is 1; then
                release reading_failed
                exit 1
            fi
        fi
    fi
    sleep "$interval"
done

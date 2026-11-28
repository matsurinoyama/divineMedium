#!/bin/sh
# The PiSugar button, given to the ritual. pisugar-server runs this (as root) at
# each press, and it passes the press on to main.py as the key it stands for:
#
#   single tap  → o   nothing
#   double tap  → e   give the covenant / end the prayer / take leave (the farewell)
#   long tap    → i   turn the covenant to the other tongue / break the ritual
#
# main.py opens button.fifo beside this script while it wakes (control.button_pipe).

case "$1" in
    single) key=o ;;
    double) key=e ;;
    long)   key=i ;;
    *) echo "usage: $0 single|double|long" >&2; exit 2 ;;
esac

FIFO="$(dirname "$(readlink -f "$0")")/button.fifo"

# main.py is not awake: the press falls on no one.
[ -p "$FIFO" ] || exit 0

# A pipe left by a main.py that crashed has no reader, and would hold the press forever.
timeout 1 sh -c 'printf %s "$1" > "$2"' _ "$key" "$FIFO"

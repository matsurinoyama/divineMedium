#!/bin/sh
# PiSugar button → divineMedium. pisugar-server runs this (as root) on a tap and
# it forwards the tap to main.py as the key the keyboard would have sent:
#
#   single tap  → o   end the prayer
#   double tap  → e   consent (on the consent form) / end the ritual (the entity gives a farewell)
#   long tap    → i   switch Japanese / English (on the consent form) / interrupt and reset
#
# main.py creates button.fifo next to this script while it runs and reads it
# alongside stdin (see control.button_pipe).

case "$1" in
    single) key=o ;;
    double) key=e ;;
    long)   key=i ;;
    *) echo "usage: $0 single|double|long" >&2; exit 2 ;;
esac

FIFO="$(dirname "$(readlink -f "$0")")/button.fifo"

# main.py isn't running — drop the tap.
[ -p "$FIFO" ] || exit 0

# A FIFO left behind by a crashed main.py has no reader and would block the write.
timeout 1 sh -c 'printf %s "$1" > "$2"' _ "$key" "$FIFO"

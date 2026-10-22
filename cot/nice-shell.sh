#!/bin/sh
# CLAUDE_CODE_SHELL_PREFIX in agent containers: Claude's commands (and what they start,
# test suites included) run at a lower priority than Claude, which stays responsive
# however hard they load the container's CPUs. Claude passes the command as one string.
exec nice -n 10 /bin/bash -c "$1"

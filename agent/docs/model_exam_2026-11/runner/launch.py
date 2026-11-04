#!/usr/bin/env python3
"""Start run.sh with INT and HUP at their defaults, keeping the same pid.

A job started with & comes up with INT ignored, nohup ignores HUP, and bash
cannot trap a signal that was ignored when it started.
"""
import os
import signal
import sys

signal.signal(signal.SIGINT, signal.SIG_DFL)
signal.signal(signal.SIGHUP, signal.SIG_DFL)
os.execv(sys.argv[1], sys.argv[1:])

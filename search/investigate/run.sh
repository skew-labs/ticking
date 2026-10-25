#!/bin/bash
# (re)start the DEV investigate copy on 127.0.0.1:8899 (never touches em-investigate.service on :8898)
P=$(ss -ltnp | grep "127.0.0.1:8899 " | grep -o "pid=[0-9]*" | cut -d= -f2); [ -n "$P" ] && kill $P && sleep 1
cd /srv/skew/em-flowmap-dev/investigate-dev
setsid nice -n 10 /srv/skew/em-galaxy/.venv/bin/python investigate.py --port 8899 </dev/null >>/srv/skew/em-flowmap-dev/logs/investigate-dev.log 2>&1 &
sleep 3; ss -ltn | grep -c "127.0.0.1:8899 "

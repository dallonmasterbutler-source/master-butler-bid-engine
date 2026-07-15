#!/bin/zsh
# THE 5-O'CLOCK TRIGGER (Dallon, Jul 14: "i was under the impression
# that all our work would start at 5 and you had a trigger").
# Runs from launchd at 5:05 PM daily: if code is pushed but not yet
# deployed (the staged-for-after-5 pattern), trigger the Render deploy
# and log it. Nothing to deploy = exits quietly.
cd /Users/dallonanderson/master-butler-bid-engine || exit 0
KEY=$(grep RENDER_API_KEY_NEW .env | cut -d= -f2)
[ -z "$KEY" ] && exit 0
git fetch -q origin main 2>/dev/null
WANT=$(git rev-parse origin/main 2>/dev/null)
LIVE=$(curl -s -m 30 "https://api.render.com/v1/services/srv-d96rkpm7r5hc7389frb0/deploys?limit=1" \
  -H "Authorization: Bearer $KEY" | /usr/bin/python3 -c \
  "import json,sys;d=json.load(sys.stdin);print(d[0]['deploy']['commit']['id'])" 2>/dev/null)
if [ -n "$WANT" ] && [ "$WANT" != "$LIVE" ]; then
  curl -s -m 30 -X POST \
    "https://api.render.com/v1/services/srv-d96rkpm7r5hc7389frb0/deploys" \
    -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
    -d '{}' -o /dev/null
  echo "$(date): after-5 trigger deployed ${WANT:0:7} (live was ${LIVE:0:7})" \
    >> data/after5_deploys.log
fi

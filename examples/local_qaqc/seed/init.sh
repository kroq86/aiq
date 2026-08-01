#!/bin/sh
set -eu

mc alias set local http://minio:9000 minioadmin minioadmin
mc mb --ignore-existing local/agentlog-lab
mc version enable local/agentlog-lab
mc cp /seed/orders.json local/agentlog-lab/datasets/orders.json
mc cp /seed/qa-rules-v17.json local/agentlog-lab/rules/qa-rules-v17.json

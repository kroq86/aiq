#!/bin/sh
set -eu

mc alias set local http://minio:9000 minioadmin minioadmin
mc mb --ignore-existing local/aiq-lab
mc version enable local/aiq-lab
mc cp /seed/orders.json local/aiq-lab/datasets/orders.json
mc cp /seed/qa-rules-v17.json local/aiq-lab/rules/qa-rules-v17.json

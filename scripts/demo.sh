#!/usr/bin/env bash
set -euo pipefail

if [[ -f .env ]]; then
    set -a
    # Compose and the demo must use the same local ports and database settings.
    source .env
    set +a
fi

API_URL="http://127.0.0.1:${API_PORT:-8000}"
MOCK_URL="http://127.0.0.1:${HELPDESK_MOCK_PORT:-8001}"
DB_USER="${POSTGRES_USER:-helpdesk}"
DB_NAME="${POSTGRES_DB:-helpdesk}"

db_scalar() {
    docker compose exec -T db psql \
        -U "$DB_USER" \
        -d "$DB_NAME" \
        -v ON_ERROR_STOP=1 \
        -Atc "$1"
}

wait_for_http() {
    local url="$1"
    local attempt
    for attempt in $(seq 1 60); do
        if curl --silent --fail "$url" >/dev/null; then
            return 0
        fi
        sleep 1
    done
    printf 'Timed out waiting for %s\n' "$url" >&2
    return 1
}

send_webhook() {
    local event_id="$1"
    local ticket_id="$2"
    local payload
    payload=$(printf \
        '{"event_id":"%s","ticket_id":"%s","customer_email":"demo@example.com","message":"Як експортувати звіт у PDF?","reply_url":"http://helpdesk-mock:8001/reply"}' \
        "$event_id" "$ticket_id")
    curl --silent --show-error --fail \
        -H 'Content-Type: application/json' \
        -d "$payload" \
        "$API_URL/webhooks/helpdesk"
}

wait_for_status() {
    local event_id="$1"
    local expected="$2"
    local status
    local attempt
    for attempt in $(seq 1 240); do
        status=$(db_scalar \
            "SELECT status FROM tickets WHERE event_id = '${event_id}';")
        if [[ "$status" == "$expected" ]]; then
            return 0
        fi
        if [[ "$status" == "failed" || "$status" == "send_failed" ]]; then
            printf 'Ticket %s entered terminal status %s\n' "$event_id" "$status" >&2
            return 1
        fi
        sleep 1
    done
    printf 'Timed out waiting for ticket %s to become %s (current: %s)\n' \
        "$event_id" "$expected" "${status:-missing}" >&2
    return 1
}

assert_one_delivery() {
    local ticket_id="$1"
    local deliveries
    local count
    deliveries=$(curl --silent --show-error --fail "$MOCK_URL/deliveries")
    printf '%s\n' "$deliveries"
    count=$(printf '%s' "$deliveries" \
        | grep -Eo "\"ticket_id\"[[:space:]]*:[[:space:]]*\"${ticket_id}\"" \
        | wc -l | tr -d '[:space:]') || true
    if [[ "${count:-0}" != "1" ]]; then
        printf 'Expected one delivery for %s, found %s\n' \
            "$ticket_id" "${count:-0}" >&2
        return 1
    fi
}

printf 'Starting API, worker, PostgreSQL, and helpdesk mock...\n'
docker compose up --build -d
wait_for_http "$API_URL/health"
wait_for_http "$MOCK_URL/health"

first_event="evt_demo_$(date +%s)_$$"
first_ticket="T-demo-$$"
printf '\nScenario 1: initial webhook plus two identical retries\n'
send_webhook "$first_event" "$first_ticket"
send_webhook "$first_event" "$first_ticket"
send_webhook "$first_event" "$first_ticket"
accepted_rows=$(db_scalar \
    "SELECT count(*) FROM tickets WHERE event_id = '${first_event}';")
printf 'Persisted rows for duplicate event: %s\n' "$accepted_rows"
if [[ "$accepted_rows" != "1" ]]; then
    printf 'Expected one persisted ticket for duplicate webhook deliveries\n' >&2
    exit 1
fi

wait_for_status "$first_event" "draft_ready"
first_uuid=$(db_scalar \
    "SELECT id FROM tickets WHERE event_id = '${first_event}';")
first_draft=$(db_scalar \
    "SELECT draft_text FROM tickets WHERE event_id = '${first_event}';")
approval_status=$(curl --silent --show-error --output /dev/null \
    --write-out '%{http_code}' \
    --request POST "$API_URL/operator/approve/$first_uuid" \
    --data-urlencode "draft_text=$first_draft")
if [[ "$approval_status" != "303" ]]; then
    printf 'Expected approval redirect 303, got %s\n' "$approval_status" >&2
    exit 1
fi
wait_for_status "$first_event" "sent"
printf 'Ticket status: %s\n' \
    "$(db_scalar "SELECT status FROM tickets WHERE event_id = '${first_event}';")"
printf 'Mock delivery ledger:\n'
assert_one_delivery "$first_ticket"

printf '\nScenario 2: high transient 5xx rate, then successful idempotent retry\n'
MOCK_FAILURE_RATE=0.8 MOCK_FAIL_BEFORE_RECORD=2 \
    docker compose up --force-recreate -d helpdesk-mock
wait_for_http "$MOCK_URL/health"
second_event="evt_demo_retry_$(date +%s)_$$"
second_ticket="T-demo-retry-$$"
send_webhook "$second_event" "$second_ticket"
wait_for_status "$second_event" "draft_ready"
second_uuid=$(db_scalar \
    "SELECT id FROM tickets WHERE event_id = '${second_event}';")
second_draft=$(db_scalar \
    "SELECT draft_text FROM tickets WHERE event_id = '${second_event}';")
approval_status=$(curl --silent --show-error --output /dev/null \
    --write-out '%{http_code}' \
    --request POST "$API_URL/operator/approve/$second_uuid" \
    --data-urlencode "draft_text=$second_draft")
if [[ "$approval_status" != "303" ]]; then
    printf 'Expected approval redirect 303, got %s\n' "$approval_status" >&2
    exit 1
fi
wait_for_status "$second_event" "sent"
second_attempts=$(db_scalar \
    "SELECT delivery_attempts FROM tickets WHERE event_id = '${second_event}';")
if (( second_attempts < 3 )); then
    printf 'Expected at least two transient failures, got %s attempts\n' \
        "$second_attempts" >&2
    exit 1
fi
printf 'Ticket status: %s after %s attempts\n' \
    "$(db_scalar "SELECT status FROM tickets WHERE event_id = '${second_event}';")" \
    "$second_attempts"
printf 'Mock delivery ledger:\n'
assert_one_delivery "$second_ticket"

printf '\nDemo complete. Operator page: %s/operator/moderation\n' "$API_URL"

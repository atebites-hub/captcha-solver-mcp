#!/usr/bin/env bash
# Smoke tests hitting the FastAPI directly on 127.0.0.1:8899.
# Requires the container to be running (./install.sh done).
set -u
SOLVER="${CAPTCHA_SOLVER_URL:-http://127.0.0.1:8899}"

pass=0
fail=0

test_case() {
    local name="$1"
    local body="$2"
    local expect_key="$3"  # either "token" or "cookies"
    local timeout="${4:-120}"

    echo "--- $name ---"
    t0=$(date +%s)
    resp=$(curl -sS --max-time "$timeout" -X POST "$SOLVER/solve" \
        -H 'Content-Type: application/json' \
        -d "$body")
    t1=$(date +%s)
    dur=$((t1 - t0))
    echo "  $dur s: $resp" | head -c 300
    echo
    if echo "$resp" | grep -q "\"$expect_key\""; then
        pass=$((pass + 1))
        echo "  ✓ PASS"
    else
        fail=$((fail + 1))
        echo "  ✗ FAIL"
    fi
    echo
}

echo "=== captcha-solver smoke tests ($SOLVER) ==="
echo

curl -sf "$SOLVER/health" && echo || { echo "solver not reachable"; exit 1; }
echo

# reCAPTCHA v2 — 2captcha demo
test_case "recaptcha_v2" \
    '{"type":"recaptcha_v2","site_url":"https://2captcha.com/demo/recaptcha-v2","site_key":"6LfD3PIbAAAAAJs_eEHvoOl75_83eXSqpPSRFJ_u","timeout_s":180}' \
    "token" 240

# reCAPTCHA v3 — Google demo
test_case "recaptcha_v3" \
    '{"type":"recaptcha_v3","site_url":"https://www.google.com/recaptcha/api2/demo","site_key":"6LeIxAcTAAAAAJcZVRqyHh71UMIEGNQ_MXjiZKhI","timeout_s":60}' \
    "token" 90

# Turnstile — nopecha demo
test_case "turnstile" \
    '{"type":"turnstile","site_url":"https://nopecha.com/demo/turnstile","site_key":"auto","timeout_s":60}' \
    "token" 90

# hCaptcha — accounts.hcaptcha.com demo
test_case "hcaptcha" \
    '{"type":"hcaptcha","site_url":"https://accounts.hcaptcha.com/demo","site_key":"00000000-0000-0000-0000-000000000000","timeout_s":180}' \
    "token" 240

echo "================================"
echo "pass: $pass    fail: $fail"
[[ $fail -eq 0 ]] && exit 0 || exit 1

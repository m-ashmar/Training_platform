#!/usr/bin/env bash
# Write deploy/.env for the single-server deployment, once.
#
# It asks for what only the owner knows (the domain, the bucket, the email provider,
# the AI provider) and GENERATES every key on this machine, so no key is ever typed,
# pasted into a chat, or sent anywhere. Secrets are read without echo and written only
# to deploy/.env, mode 600.
#
# It NEVER overwrites an existing deploy/.env: that file holds FIELD_ENCRYPTION_KEY,
# and replacing it makes the health data already in the database unreadable for ever.
# To change one value later, edit the file (`nano deploy/.env`) and run
# `docker compose up -d`.
#
# Needs: bash and openssl (both on a stock Ubuntu or Debian server).
set -euo pipefail
umask 077

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
target="$here/.env"

if [ -e "$target" ]; then
    echo "deploy/.env already exists, and this script never overwrites it." >&2
    echo "Edit it with:  nano $target" >&2
    exit 1
fi
command -v openssl >/dev/null 2>&1 || { echo "openssl is not installed: apt-get install -y openssl" >&2; exit 1; }

# --- asking ------------------------------------------------------------------------
# A value goes into the file inside 'single quotes', which Docker Compose reads
# literally. So the one character a value may not contain is a single quote.
clean() {
    case "$1" in
        *"'"*|*$'\n'*|*$'\r'*) return 1 ;;
        *) return 0 ;;
    esac
}

ask() {            # ask VAR "question" [default]
    local __var="$1" __question="$2" __default="${3-}" __answer
    while :; do
        if [ -n "$__default" ]; then
            read -r -p "$__question [$__default]: " __answer || true
            __answer="${__answer:-$__default}"
        else
            read -r -p "$__question: " __answer || true
        fi
        __answer="$(printf '%s' "$__answer" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
        if [ -z "$__answer" ]; then echo "  This one is needed."; [ -t 0 ] || exit 1; continue; fi
        if ! clean "$__answer"; then echo "  It cannot contain a ' character."; [ -t 0 ] || exit 1; continue; fi
        printf -v "$__var" '%s' "$__answer"
        return 0
    done
}

ask_optional() {   # ask_optional VAR "question"   (Enter leaves it empty)
    local __var="$1" __question="$2" __answer
    read -r -p "$__question (Enter to skip): " __answer || true
    __answer="$(printf '%s' "$__answer" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
    clean "$__answer" || { echo "  It cannot contain a ' character; skipped."; __answer=""; }
    printf -v "$__var" '%s' "$__answer"
}

ask_secret() {     # ask_secret VAR "question" [optional]   (typed or pasted, not shown)
    local __var="$1" __question="$2" __optional="${3-}" __answer
    while :; do
        read -r -s -p "$__question (hidden${__optional:+, Enter to skip}): " __answer || true
        echo
        __answer="$(printf '%s' "$__answer" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
        if [ -z "$__answer" ] && [ -z "$__optional" ]; then echo "  This one is needed."; [ -t 0 ] || exit 1; continue; fi
        if ! clean "$__answer"; then echo "  It cannot contain a ' character."; [ -t 0 ] || exit 1; continue; fi
        printf -v "$__var" '%s' "$__answer"
        return 0
    done
}

echo "Yalla Gym: single-server setup. Nothing is sent anywhere; this writes deploy/.env."
echo

echo "1. Where it runs"
ask API_DOMAIN  "  The API's host name, already pointing at this server (e.g. api.yallagym.com)"
case "$API_DOMAIN" in
    http://*|https://*|*/*|*" "*) echo "  Give the host name only, without https:// or a path." >&2; exit 1 ;;
    localhost*|127.*|*"*"*)        echo "  It must be a real public host name." >&2; exit 1 ;;
esac
ask ACME_EMAIL  "  Your email, for certificate expiry notices from Let's Encrypt"
echo

echo "2. Photos and videos: the S3-compatible bucket (it must be PRIVATE)"
ask S3_ENDPOINT_URL "  The store's S3 address" "https://fsn1.your-objectstorage.com"
case "$S3_ENDPOINT_URL" in
    https://*) ;;
    *) echo "  It must start with https://" >&2; exit 1 ;;
esac
S3_ENDPOINT_URL="${S3_ENDPOINT_URL%/}"
guessed_region="$(printf '%s' "$S3_ENDPOINT_URL" | sed -n 's#^https://\([a-z0-9-]*\)\.your-objectstorage\.com$#\1#p')"
ask S3_REGION_NAME  "  Its region" "${guessed_region:-us-east-1}"
ask S3_BUCKET_NAME  "  The bucket's name"
ask_secret S3_ACCESS_KEY_ID     "  The bucket's access key"
ask_secret S3_SECRET_ACCESS_KEY "  The bucket's secret key"
echo

echo "3. Email, for sign-up codes"
ask EMAIL_HOST          "  SMTP host" "smtp.resend.com"
ask EMAIL_HOST_USER     "  SMTP user" "resend"
ask_secret EMAIL_HOST_PASSWORD "  SMTP password (for Resend: the API key)"
ask DEFAULT_FROM_EMAIL  "  The From address, on your verified domain" "Yalla Gym <no-reply@${API_DOMAIN#api.}>"
echo

echo "4. The AI coach (skip the key and the coach answers 'unavailable' until you add it)"
ask_secret AI_API_KEY "  The AI provider's key" optional
AI_BASE_URL=""; AI_ASSISTANT_MODEL=""; AI_PRICE_IN=""; AI_PRICE_OUT=""
if [ -n "$AI_API_KEY" ]; then
    ask AI_BASE_URL        "  The provider's address, ending in /v1"
    ask AI_ASSISTANT_MODEL "  The model's name at that provider"
    ask_optional AI_PRICE_IN  "  Price per 1M input tokens, in USD"
    if [ -n "$AI_PRICE_IN" ]; then
        ask AI_PRICE_OUT "  Price per 1M output tokens, in USD"
    fi
fi
echo

echo "5. Error alerts"
ask_secret SENTRY_DSN "  The Sentry DSN" optional
echo

# --- generating --------------------------------------------------------------------
echo "Generating the keys on this server..."
hex() { openssl rand -hex "$1"; }
DB_PASSWORD="$(hex 24)"
REDIS_PASSWORD="$(hex 24)"
DJANGO_SECRET_KEY="$(hex 48)"
AI_TRAINING_PSEUDONYM_KEY="$(hex 32)"
# A Fernet key: 32 random bytes, URL-safe base64.
FIELD_ENCRYPTION_KEY="$(openssl rand -base64 32 | tr '+/' '-_' | tr -d '\n')"
JWT_PRIVATE_KEY="$(openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 2>/dev/null)"
JWT_PUBLIC_KEY="$(printf '%s\n' "$JWT_PRIVATE_KEY" | openssl pkey -pubout 2>/dev/null)"
case "$JWT_PRIVATE_KEY" in "-----BEGIN PRIVATE KEY-----"*) ;; *) echo "openssl did not produce a private key" >&2; exit 1 ;; esac
case "$JWT_PUBLIC_KEY"  in "-----BEGIN PUBLIC KEY-----"*)  ;; *) echo "openssl did not produce a public key" >&2; exit 1 ;; esac

# --- writing -----------------------------------------------------------------------
tmp="$(mktemp "$here/.env.XXXXXX")"
trap 'rm -f "$tmp"' EXIT
line() { printf "%s='%s'\n" "$1" "$2" >> "$tmp"; }

{
    echo "# Written by deploy/setup.sh on $(date -u +%F). Keep a copy in the password manager:"
    echo "# FIELD_ENCRYPTION_KEY and AI_TRAINING_PSEUDONYM_KEY cannot be replaced."
    echo "# Change a value: edit this file, then 'docker compose up -d'."
} >> "$tmp"
line API_DOMAIN "$API_DOMAIN"
line ACME_EMAIL "$ACME_EMAIL"
line DJANGO_ALLOWED_HOSTS "$API_DOMAIN"
line CORS_ALLOWED_ORIGINS "https://$API_DOMAIN"
line CSRF_TRUSTED_ORIGINS "https://$API_DOMAIN"
line SENTRY_ENVIRONMENT "staging"
line DB_NAME "yallagym"
line DB_USER "yallagym"
line DB_PASSWORD "$DB_PASSWORD"
line REDIS_PASSWORD "$REDIS_PASSWORD"
line DJANGO_SECRET_KEY "$DJANGO_SECRET_KEY"
line FIELD_ENCRYPTION_KEY "$FIELD_ENCRYPTION_KEY"
line AI_TRAINING_PSEUDONYM_KEY "$AI_TRAINING_PSEUDONYM_KEY"
printf 'JWT_PRIVATE_KEY="%s"\n' "$JWT_PRIVATE_KEY" >> "$tmp"
printf 'JWT_PUBLIC_KEY="%s"\n'  "$JWT_PUBLIC_KEY"  >> "$tmp"
line S3_ENDPOINT_URL "$S3_ENDPOINT_URL"
line S3_REGION_NAME "$S3_REGION_NAME"
line S3_BUCKET_NAME "$S3_BUCKET_NAME"
line S3_ACCESS_KEY_ID "$S3_ACCESS_KEY_ID"
line S3_SECRET_ACCESS_KEY "$S3_SECRET_ACCESS_KEY"
line EMAIL_HOST "$EMAIL_HOST"
line EMAIL_HOST_USER "$EMAIL_HOST_USER"
line EMAIL_HOST_PASSWORD "$EMAIL_HOST_PASSWORD"
line DEFAULT_FROM_EMAIL "$DEFAULT_FROM_EMAIL"
# An optional variable is written only when it has a value: an EMPTY one is not "unset"
# to the settings, it is the empty string, and replaces the default.
if [ -n "$AI_API_KEY" ]; then
    line AI_API_KEY "$AI_API_KEY"
    line AI_BASE_URL "$AI_BASE_URL"
    line AI_ASSISTANT_MODEL "$AI_ASSISTANT_MODEL"
    if [ -n "$AI_PRICE_IN" ]; then
        line AI_PRICE_INPUT_PER_1M_USD "$AI_PRICE_IN"
        line AI_PRICE_OUTPUT_PER_1M_USD "$AI_PRICE_OUT"
    fi
fi
if [ -n "$SENTRY_DSN" ]; then line SENTRY_DSN "$SENTRY_DSN"; fi

chmod 600 "$tmp"
mv "$tmp" "$target"
trap - EXIT

cat <<DONE

Wrote $target (mode 600).

Next:
  1. Save a copy of that file in your password manager now. It holds keys that
     cannot be made again.
  2. docker compose build web
  3. docker compose up -d
  4. docker compose ps -a     (migrate shows "Exited (0)", the rest "Up")
DONE

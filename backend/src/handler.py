import json
import urllib.request
import urllib.error
import hashlib
import boto3
import os
import time
import re
from constants import Constants

# ─── Claude classification prompt ───────────────────────────────────────────

SYSTEM_PROMPT = """ You are an AI Ticket Classifier for a modern SaaS product company.

Your task is to classify customer support tickets using all information provided in the request.

---

## Input

The input will be a JSON object representing a support ticket.

Use **all available fields** (subject, description, product, attachments, metadata, customer tier, etc.) when determining the classification.

---

## Classification Rules

Determine:

* category
* subcategory
* priority
* severity
* team
* sentiment
* customer_impact
* confidence
* reason
* tags

Use the following categories:

* authentication
* billing
* account-management
* bug
* performance
* outage
* integration
* security
* data
* deployment
* feature-request
* documentation
* onboarding
* general-support

Assign the appropriate subcategory for the selected category.

---

## Priority

Choose one.

P0 = Critical production outage or security incident

P1 = Major functionality unavailable

P2 = Important issue affecting core functionality

P3 = Minor issue

P4 = Feature request or general inquiry

---

## Severity

Map priority as follows:

P0 → critical

P1 → high

P2 → medium

P3 → low

P4 → informational

---

## Customer Impact

Choose one:

* single-customer
* few-customers
* organization-wide
* multiple-customers
* global

---

## Sentiment

Choose one:

* positive
* neutral
* negative
* frustrated
* urgent

---

## Confidence

Return a value between 0.00 and 1.00.

---

## Reason

One concise sentence explaining why the ticket was classified that way.

---

## Tags

Return 2–6 relevant lowercase tags.

---

## Output

Return ONLY valid JSON.

Preserve the original request under the **request** field.

Add a new **classification** object containing the AI prediction.

"""

# ─── SSM parameter cache ────────────────────────────────────────────────────
# Caches SSM values in memory so we only call SSM once per Lambda warm start
# not on every request

_param_cache = {}

def get_ssm_parameter(name):
    if name not in _param_cache:
        ssm = boto3.client('ssm')
        response = ssm.get_parameter(Name=name, WithDecryption=True)
        _param_cache[name] = response['Parameter']['Value']
    return _param_cache[name]

def sanitise_body(text):
    """Remove PII from ticket body before hashing or sending to Claude."""

    # Remove email addresses
    text = re.sub(
        r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}',
        '[email]',
        text
    )

    # Remove phone numbers (various formats)
    text = re.sub(
        r'[\+]?[(]?[0-9]{3}[)]?[\s\-\.]?[0-9]{3}[\s\-\.]?[0-9]{4,6}',
        '[phone]',
        text
    )

    # Remove credit card numbers (groups of 4 digits)
    text = re.sub(
        r'\b(?:\d{4}[\s\-]?){3}\d{4}\b',
        '[card]',
        text
    )

    return text.strip()

# ─── Upstash Redis helpers ───────────────────────────────────────────────────

def upstash_command(url, token, *args):
    """Send a Redis command to Upstash via REST API."""
    req = urllib.request.Request(
        url,
        data=json.dumps(list(args)).encode('utf-8'),
        headers={
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json'
        },
        method='POST'
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return json.loads(response.read().decode('utf-8'))
    except Exception as e:
        print(f"Upstash error: {str(e)}")
        return {}

def check_cache(url, token, cache_key):
    """Check if a classification result is cached. Returns dict or None."""
    result = upstash_command(url, token, 'GET', cache_key)
    if result.get('result'):
        return json.loads(result['result'])
    return None

def set_cache(url, token, cache_key, value):
    """Cache a classification result for 7 days."""
    upstash_command(url, token, 'SET', cache_key, json.dumps(value), 'EX', 604800)

def is_circuit_open(url, token):
    """Return True if circuit is open (Claude should not be called)."""
    result = upstash_command(url, token, 'GET', Constants.CIRCUIT_OPEN_KEY)
    if result.get('result'):
        print("Circuit is OPEN — blocking Claude call, returning 503")
        return True
    print("Circuit is CLOSED — proceeding with Claude call")
    return False

def record_failure(url, token):
    """Increment failure counter. Open circuit if threshold reached."""
    upstash_command(url, token, 'INCR', Constants.CIRCUIT_FAILURE_KEY)
    upstash_command(url, token, 'EXPIRE', Constants.CIRCUIT_FAILURE_KEY, Constants.FAILURE_WINDOW_TTL)

    result = upstash_command(url, token, 'GET', Constants.CIRCUIT_FAILURE_KEY)
    count = int(result.get('result') or 0)
    print(f"Failure count: {count}/{Constants.FAILURE_THRESHOLD}")

    if count >= Constants.FAILURE_THRESHOLD:
        upstash_command(url, token, 'SET', Constants.CIRCUIT_OPEN_KEY, '1', 'EX', Constants.CIRCUIT_OPEN_TTL)
        print(f"CIRCUIT OPENED — blocking all calls for {Constants.CIRCUIT_OPEN_TTL} seconds")
        publish_metric('CircuitBreakerOpen', 1)

def record_success(url, token):
    """Reset failure counter on successful Claude call."""
    result = upstash_command(url, token, 'GET', Constants.CIRCUIT_OPEN_KEY)
    was_open = bool(result.get('result'))

    upstash_command(url, token, 'DEL', Constants.CIRCUIT_FAILURE_KEY)

    if was_open:
        print("Circuit CLOSED — Claude recovered")
        publish_metric('CircuitBreakerClose', 1)
# ─── Claude API call ─────────────────────────────────────────────────────────

def call_claude(api_key, ticket_body):
    """Call Claude API and return parsed classification JSON."""
    url = 'https://api.anthropic.com/v1/messages'

    payload = {
        'model': 'claude-haiku-4-5-20251001',
        'max_tokens': 500,
        'system': SYSTEM_PROMPT,
        'messages': [
            {
                'role': 'user',
                'content': f'Classify this support ticket:\n\n{ticket_body}'
            }
        ]
    }

    headers = {
        'Content-Type': 'application/json',
        'x-api-key': api_key,
        'anthropic-version': '2023-06-01'
    }

    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers=headers, method='POST')

    with urllib.request.urlopen(req, timeout=30) as response:
        result = json.loads(response.read().decode('utf-8'))
        raw_text = result['content'][0]['text'].strip()

        # Remove backticks if Claude accidentally added them
        if raw_text.startswith('```'):
            raw_text = raw_text.split('```')[1]
            if raw_text.startswith('json'):
                raw_text = raw_text[4:]

        return json.loads(raw_text.strip())

def call_claude_with_retry(api_key, ticket_body):
    """Call Claude with exponential backoff retry. Max 3 attempts."""
    last_error = None

    for attempt in range(Constants.MAX_RETRIES):
        try:
            print(f"Claude API attempt {attempt + 1}/{Constants.MAX_RETRIES}")
            start = time.time()
            result = call_claude(api_key, ticket_body)
            latency = int((time.time() - start) * 1000)
            print(f"Claude responded in {latency}ms")
            publish_metric('ClaudeLatency', latency, unit='Milliseconds')
            return result

        except Exception as e:
            last_error = e
            wait = 2 ** attempt  # 1s, 2s

            if attempt < Constants.MAX_RETRIES - 1:
                print(f"Attempt {attempt + 1}/{Constants.MAX_RETRIES} failed: {str(e)} — retrying in {wait}s")
                time.sleep(wait)
            else:
                print(f"Attempt {attempt + 1}/{Constants.MAX_RETRIES} failed: {str(e)} — giving up")

    raise last_error

# ─── CloudWatch metrics ───────────────────────────────────────────────────────

def publish_metric(metric_name, value, unit='Count'):
    """Publish a custom metric to CloudWatch."""
    try:
        namespace = os.environ.get('CLOUDWATCH_NAMESPACE', 'TicketClassifier')
        cw = boto3.client('cloudwatch')
        cw.put_metric_data(
            Namespace=namespace,
            MetricData=[
                {
                    'MetricName': metric_name,
                    'Value': value,
                    'Unit': unit
                }
            ]
        )
    except Exception as e:
        # Never let metric publishing break the main flow
        print(f"CloudWatch metric error (non-fatal): {str(e)}")

# ─── DynamoDB save ───────────────────────────────────────────────────────────

def save_to_dynamodb(table_name, ticket_id, classification_id, ticket_body, classification, from_cache):
    """Save classification result permanently to DynamoDB."""
    dynamodb = boto3.resource('dynamodb')
    table = dynamodb.Table(table_name)

    confidence = float(classification.get('confidence', 0))
    action = 'auto-route' if confidence >= 0.80 else 'human-review'
    ttl = int(time.time()) + (365 * 24 * 60 * 60)  # 1 year from now

    table.put_item(Item={
        'ticket_id': ticket_id,
        'classification_id': classification_id,
        'body_preview': ticket_body[:200],
        'ai_category': classification.get('category', 'general'),
        'ai_sub_category': classification.get('subcategory', ''),
        'ai_priority': classification.get('priority', 'P3'),
        'ai_severity': classification.get('severity', 'medium'),
        'ai_team': classification.get('team', 'general-support'),
        'ai_sentiment':classification.get('sentiment', ''),
        'ai_confidence': str(confidence),
        'ai_reason': classification.get('reason', ''),
        'ai_tags': classification.get('tags', ['']),
        'ai_action': action,
        'from_cache': from_cache,
        'created_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'TTL': ttl
    })

    return action

# ─── Main Lambda handler ─────────────────────────────────────────────────────

def lambda_handler(event, context):
    path = event.get('path', '')
    method = event.get('httpMethod', 'GET')

    # ── Health check ──────────────────────────────────────────────────────────
    if path == '/health':
        return response(200, {'status': 'healthy', 'service': 'ticket-classifier'})

    # ── Classify ticket ───────────────────────────────────────────────────────
    if path == '/v1/classify' and method == 'POST':
        try:
            body = json.loads(event.get('body') or '{}')
            ticket_id = body.get('ticket_id', '').strip()
            ticket_body = body.get('body', '').strip()

            # Input validation
            if not ticket_id:
                return response(400, {'error': 'ticket_id is required'})
            if not ticket_body:
                return response(400, {'error': 'body is required'})
            if len(ticket_body) > 5000:
                return response(400, {'error': 'body must be under 5000 characters'})

            # Load credentials from SSM (cached after first load)
            claude_key   = get_ssm_parameter('/ticket-classifier/claude-api-key')
            upstash_url   = get_ssm_parameter('/ticket-classifier/upstash-redis-url')
            upstash_token = get_ssm_parameter('/ticket-classifier/upstash-redis-token')
            table_name    = os.environ.get('DYNAMODB_TABLE', 'ticket-classifications-dev')

            cleaned_body = sanitise_body(ticket_body)
            print(f"Sanitised body: {cleaned_body[:100]}")

            # Compute cache key — SHA256 of lowercased ticket body
            cache_key = hashlib.sha256(cleaned_body.lower().encode()).hexdigest()[:32]

            start_time = time.time()
            from_cache = False

            # Check Redis cache
            print(f"Checking cache for key: {cache_key}")
            classification = check_cache(upstash_url, upstash_token, cache_key)

            if classification:
                from_cache = True
                print("Cache HIT — skipping Claude API call")
                publish_metric('CacheHit', 1)

            else:
                print("Cache MISS — calling Claude API")
                publish_metric('CacheMiss', 1)

                if is_circuit_open(upstash_url, upstash_token):
                    return response(503, {
                        'error': 'Classification service temporarily unavailable',
                        'retry_after': Constants.CIRCUIT_OPEN_TTL
                    })

                    # Call Claude with retry
                try:
                    classification = call_claude_with_retry(claude_key,cleaned_body)
                    record_success(upstash_url, upstash_token)
                    set_cache(upstash_url, upstash_token, cache_key, classification)

                except Exception as e:
                    print(f"All Claude attempts failed: {str(e)}")
                    record_failure(upstash_url, upstash_token)
                    publish_metric('ClassificationError', 1)
                    return response(503, {
                        'error': 'Classification service temporarily unavailable'
                    })

            latency_ms = int((time.time() - start_time) * 1000)

            # Generate unique classification ID
            classification_id = f"cls_{cache_key[:8]}_{int(time.time())}"

            # Save to DynamoDB
            action = save_to_dynamodb(
                table_name, ticket_id, classification_id,
                ticket_body, classification.get('classification',{}), from_cache
            )

            print(f"Saved to DynamoDB: {classification_id}")

            return response(200, {
                'classification_id': classification_id,
                'ticket_id': ticket_id,
                'category': classification.get('classification',{}).get('category'),
                'sub_category': classification.get('classification',{}).get('subcategory'),
                'priority': classification.get('classification',{}).get('priority'),
                'team': classification.get('classification',{}).get('team'),
                'confidence': classification.get('classification',{}).get('confidence'),
                'reason': classification.get('classification',{}).get('reason'),
                'action': action,
                'from_cache': from_cache,
                'latency_ms': latency_ms
            })

        except urllib.error.HTTPError as e:
            error_detail = e.read().decode('utf-8')
            print(f"Claude API HTTP error {e.code}: {error_detail}")
            return response(503, {'error': 'Classification service temporarily unavailable'})

        except json.JSONDecodeError as e:
            print(f"Failed to parse Claude response as JSON: {str(e)}")
            return response(500, {'error': 'Classification failed — invalid AI response'})

        except Exception as e:
            print(f"Unexpected error: {str(e)}")
            return response(500, {'error': 'Internal server error'})

    return response(404, {'error': f'Route not found: {method} {path}'})


def response(status_code, body):
    return {
        'statusCode': status_code,
        'headers': {'Content-Type': 'application/json'},
        'body': json.dumps(body)
    }
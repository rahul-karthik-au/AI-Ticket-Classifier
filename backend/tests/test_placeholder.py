import json
import sys
import os
import hashlib
from unittest.mock import patch, MagicMock



sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../src'))
import handler
# ─── Helper to build a classify event ────────────────────────────────────────

def classify_event(ticket_id,body):
    return {
        'path': '/v1/classify',
        'httpMethod': 'POST',
        'body': json.dumps({'ticket_id': ticket_id, 'body': body})
    }

# ─── Mock Claude response ─────────────────────────────────────────────────────

MOCK_CLASSIFICATION = {
    'request':{},
    'classification':{
    'category': 'billing',
    'sub_category': 'duplicate-charge',
    'priority': 'P2',
    'team': 'billing-support',
    'confidence': 0.96,
    'reason': 'Customer reports being charged twice',
    'tags': ['billing', 'duplicate-charge']
    }
}

# ─── Input validation tests ───────────────────────────────────────────────────

def test_missing_ticket_id():
    event = {
        'path': '/v1/classify',
        'httpMethod': 'POST',
        'body': json.dumps({'body': 'some ticket text'})
    }
    result = handler.lambda_handler(event, {})
    assert result['statusCode'] == 400
    body = json.loads(result['body'])
    assert 'ticket_id' in body['error']


def test_missing_body():
    event = {
        'path': '/v1/classify',
        'httpMethod': 'POST',
        'body': json.dumps({'ticket_id': 'T-001'})
    }
    result = handler.lambda_handler(event, {})
    assert result['statusCode'] == 400
    body = json.loads(result['body'])
    assert 'body' in body['error']


def test_empty_body():
    event = classify_event('T-001', '   ')
    result = handler.lambda_handler(event, {})
    assert result['statusCode'] == 400


def test_body_too_long():
    event = classify_event('T-001', 'x' * 5001)
    result = handler.lambda_handler(event, {})
    assert result['statusCode'] == 400


# ─── Health check ─────────────────────────────────────────────────────────────

def test_health_check():
    event = {'path': '/health', 'httpMethod': 'GET'}
    result = handler.lambda_handler(event, {})
    assert result['statusCode'] == 200
    body = json.loads(result['body'])
    assert body['status'] == 'healthy'


# ─── Unknown route ────────────────────────────────────────────────────────────

def test_unknown_route():
    event = {'path': '/unknown', 'httpMethod': 'GET'}
    result = handler.lambda_handler(event, {})
    assert result['statusCode'] == 404


# ─── Hash tests ───────────────────────────────────────────────────────────────

def test_same_text_produces_same_hash():
    text = "I was charged twice this month"
    hash1 = hashlib.sha256(text.lower().encode()).hexdigest()[:32]
    hash2 = hashlib.sha256(text.lower().encode()).hexdigest()[:32]
    assert hash1 == hash2


def test_different_text_produces_different_hash():
    hash1 = hashlib.sha256("billing issue".encode()).hexdigest()[:32]
    hash2 = hashlib.sha256("login problem".encode()).hexdigest()[:32]
    assert hash1 != hash2


# ─── Response helper ──────────────────────────────────────────────────────────

def test_response_helper():
    result = handler.response(200, {'key': 'value'})
    assert result['statusCode'] == 200
    assert result['headers']['Content-Type'] == 'application/json'
    body = json.loads(result['body'])
    assert body['key'] == 'value'


def test_response_sets_correct_status_code():
    result = handler.response(400, {'error': 'bad request'})
    assert result['statusCode'] == 400


# ─── PII sanitisation tests ───────────────────────────────────────────────────

def test_pii_removes_email():
    result = handler.sanitise_body("Contact me at john@example.com about my issue")
    assert 'john@example.com' not in result
    assert '[email]' in result


def test_pii_removes_phone():
    result = handler.sanitise_body("Call me at 555-123-4567 to discuss")
    assert '555-123-4567' not in result
    assert '[phone]' in result


def test_pii_removes_credit_card():
    result = handler.sanitise_body("My card 4111-1111-1111-1111 was charged twice")
    assert '4111-1111-1111-1111' not in result
    assert '[card]' in result


def test_pii_removes_multiple_patterns():
    text = "Email john@test.com, card 4111-1111-1111-1111, phone 5551234567"
    result = handler.sanitise_body(text)
    assert 'john@test.com' not in result
    assert '4111111111111111' not in result
    assert '5551234567' not in result
    assert '[email]' in result
    assert '[card]' in result
    assert '[phone]' in result


def test_clean_text_unchanged():
    text = "I was charged twice this month and need a refund"
    result = handler.sanitise_body(text)
    assert result == text


def test_sanitised_hash_is_consistent():
    text = "Email me at test@example.com about my charge"
    sanitised1 = handler.sanitise_body(text)
    sanitised2 = handler.sanitise_body(text)
    hash1 = hashlib.sha256(sanitised1.lower().encode()).hexdigest()[:32]
    hash2 = hashlib.sha256(sanitised2.lower().encode()).hexdigest()[:32]
    assert hash1 == hash2


# ─── Circuit breaker tests ────────────────────────────────────────────────────

def test_circuit_closed_when_no_redis_key():
    with patch('handler.upstash_command') as mock_redis:
        mock_redis.return_value = {'result': None}
        result = handler.is_circuit_open('http://fake-url', 'fake-token')
        assert result is False


def test_circuit_open_when_redis_key_exists():
    with patch('handler.upstash_command') as mock_redis:
        mock_redis.return_value = {'result': '1'}
        result = handler.is_circuit_open('http://fake-url', 'fake-token')
        assert result is True


def test_returns_503_when_circuit_open():
    with patch('handler.get_ssm_parameter') as mock_ssm, \
         patch('handler.is_circuit_open') as mock_circuit, \
         patch('handler.check_cache') as mock_cache, \
         patch('handler.publish_metric'):

        mock_ssm.return_value = 'fake-value'
        mock_cache.return_value = None       # cache miss
        mock_circuit.return_value = True     # circuit is open

        event = classify_event('T-001', 'I was charged twice')
        result = handler.lambda_handler(event, {})

        assert result['statusCode'] == 503
        body = json.loads(result['body'])
        assert 'retry_after' in body


def test_retry_after_value_in_503_response():
    with patch('handler.get_ssm_parameter') as mock_ssm, \
         patch('handler.is_circuit_open') as mock_circuit, \
         patch('handler.check_cache') as mock_cache, \
         patch('handler.publish_metric'):

        mock_ssm.return_value = 'fake-value'
        mock_cache.return_value = None
        mock_circuit.return_value = True

        event = classify_event('T-001', 'App crashes on login')
        result = handler.lambda_handler(event, {})

        body = json.loads(result['body'])
        assert body['retry_after'] == 30


# ─── Full classification flow test ───────────────────────────────────────────

def test_full_classification_returns_200():
    with patch('handler.get_ssm_parameter') as mock_ssm, \
         patch('handler.check_cache') as mock_cache, \
         patch('handler.is_circuit_open') as mock_circuit, \
         patch('handler.call_claude_with_retry') as mock_claude, \
         patch('handler.record_success'), \
         patch('handler.set_cache'), \
         patch('handler.save_to_dynamodb') as mock_ddb, \
         patch('handler.publish_metric'):

        mock_ssm.return_value = 'fake-value'
        mock_cache.return_value = None
        mock_circuit.return_value = False
        mock_claude.return_value = MOCK_CLASSIFICATION
        mock_ddb.return_value = 'auto-route'

        event = classify_event('T-001', 'I was charged twice this month')
        result = handler.lambda_handler(event, {})

        assert result['statusCode'] == 200
        body = json.loads(result['body'])
        print(body)
        assert body['category'] == 'billing'
        assert body['priority'] == 'P2'
        assert body['from_cache'] is False


def test_cache_hit_returns_200_without_calling_claude():
    with patch('handler.get_ssm_parameter') as mock_ssm, \
         patch('handler.check_cache') as mock_cache, \
         patch('handler.call_claude_with_retry') as mock_claude, \
         patch('handler.save_to_dynamodb') as mock_ddb, \
         patch('handler.publish_metric'):

        mock_ssm.return_value = 'fake-value'
        mock_cache.return_value = MOCK_CLASSIFICATION
        mock_ddb.return_value = 'auto-route'

        event = classify_event('T-001', 'I was charged twice this month')
        result = handler.lambda_handler(event, {})

        assert result['statusCode'] == 200
        body = json.loads(result['body'])
        assert body['from_cache'] is True
        mock_claude.assert_not_called()  # Claude should NOT be called


def test_all_new_fields_in_response():
    with patch('handler.get_ssm_parameter') as mock_ssm, \
         patch('handler.check_cache') as mock_cache, \
         patch('handler.is_circuit_open') as mock_circuit, \
         patch('handler.call_claude_with_retry') as mock_claude, \
         patch('handler.record_success'), \
         patch('handler.set_cache'), \
         patch('handler.save_to_dynamodb') as mock_ddb, \
         patch('handler.publish_metric'):

        mock_ssm.return_value = 'fake-value'
        mock_cache.return_value = None
        mock_circuit.return_value = False
        mock_claude.return_value = MOCK_CLASSIFICATION
        mock_ddb.return_value = 'auto-route'

        event = classify_event('T-001', 'I was charged twice')
        result = handler.lambda_handler(event, {})

        body = json.loads(result['body'])
        required_fields = [
            'classification_id', 'ticket_id', 'category', 'sub_category',
            'priority', 'team',
            'confidence', 'reason', 'action', 'from_cache', 'latency_ms'
        ]
        for field in required_fields:
            assert field in body, f"Missing field: {field}"
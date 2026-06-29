import json
import sys
import os

# Add the src folder to path so we can import handler
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../src'))

import handler


# ─── Test input validation ────────────────────────────────────────────────────

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
    event = {
        'path': '/v1/classify',
        'httpMethod': 'POST',
        'body': json.dumps({'ticket_id': 'T-001', 'body': '   '})
    }
    result = handler.lambda_handler(event, {})
    assert result['statusCode'] == 400


def test_body_too_long():
    event = {
        'path': '/v1/classify',
        'httpMethod': 'POST',
        'body': json.dumps({'ticket_id': 'T-001', 'body': 'x' * 5001})
    }
    result = handler.lambda_handler(event, {})
    assert result['statusCode'] == 400


# ─── Test health check ────────────────────────────────────────────────────────

def test_health_check():
    event = {'path': '/health', 'httpMethod': 'GET'}
    result = handler.lambda_handler(event, {})
    assert result['statusCode'] == 200
    body = json.loads(result['body'])
    assert body['status'] == 'healthy'


# ─── Test route not found ─────────────────────────────────────────────────────

def test_unknown_route():
    event = {'path': '/unknown', 'httpMethod': 'GET'}
    result = handler.lambda_handler(event, {})
    assert result['statusCode'] == 404


# ─── Test hash computation ────────────────────────────────────────────────────

def test_same_text_produces_same_hash():
    import hashlib
    text = "I was charged twice this month"
    hash1 = hashlib.sha256(text.lower().encode()).hexdigest()[:32]
    hash2 = hashlib.sha256(text.lower().encode()).hexdigest()[:32]
    assert hash1 == hash2


def test_different_text_produces_different_hash():
    import hashlib
    hash1 = hashlib.sha256("billing issue".encode()).hexdigest()[:32]
    hash2 = hashlib.sha256("login problem".encode()).hexdigest()[:32]
    assert hash1 != hash2


# ─── Test Claude response parsing ────────────────────────────────────────────

def test_response_helper():
    result = handler.response(200, {'key': 'value'})
    assert result['statusCode'] == 200
    assert result['headers']['Content-Type'] == 'application/json'
    body = json.loads(result['body'])
    assert body['key'] == 'value'


def test_response_sets_correct_status_code():
    result = handler.response(400, {'error': 'bad request'})
    assert result['statusCode'] == 400
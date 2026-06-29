class Constants:
    MAX_RETRIES = 3
    CIRCUIT_FAILURE_KEY = "circuit:failures"
    CIRCUIT_OPEN_KEY = "circuit:open"
    FAILURE_THRESHOLD = 5
    CIRCUIT_OPEN_TTL = 30  # seconds the circuit stays open
    FAILURE_WINDOW_TTL = 60  # seconds before failure counter resets
    CACHE_TTL = 604800  # 7 days in seconds
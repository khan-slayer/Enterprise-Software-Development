"""
caller_http.py - Resilient HTTP Client with Timeout, Exponential Backoff Retry,
Idempotency Key Protection, and a 3-State Circuit Breaker.

Enterprise Software Development - Assignment 2 (Part A)
"""

import time
import sys
import uuid
import logging
from enum import Enum
from typing import Optional, Dict, Any
import requests

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("HTTPCaller")

class CircuitState(Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"

class CircuitBreakerOpenException(Exception):
    """Raised when an outbound call is blocked because the circuit breaker is OPEN."""
    pass

class CircuitBreaker:
    def __init__(self, failure_threshold: int = 3, recovery_timeout: float = 4.0):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.state = CircuitState.CLOSED
        self.consecutive_failures = 0
        self.last_state_change = time.time()

    def record_success(self):
        if self.state in (CircuitState.HALF_OPEN, CircuitState.CLOSED):
            if self.state == CircuitState.HALF_OPEN:
                logger.info(f"🔄 [CircuitBreaker] Probe call succeeded. Transitioning {self.state.value} -> CLOSED.")
            self.state = CircuitState.CLOSED
            self.consecutive_failures = 0

    def record_failure(self):
        self.consecutive_failures += 1
        logger.warning(f"⚠️ [CircuitBreaker] Failure recorded (count: {self.consecutive_failures}/{self.failure_threshold}).")
        if self.state == CircuitState.HALF_OPEN or self.consecutive_failures >= self.failure_threshold:
            self.state = CircuitState.OPEN
            self.last_state_change = time.time()
            logger.error(f"🚨 [CircuitBreaker] Threshold reached! State changed to OPEN. Outbound calls blocked for {self.recovery_timeout}s.")

    def allow_request(self) -> bool:
        if self.state == CircuitState.CLOSED:
            return True
        now = time.time()
        if self.state == CircuitState.OPEN:
            if (now - self.last_state_change) >= self.recovery_timeout:
                logger.info(f"⏳ [CircuitBreaker] Recovery window elapsed ({self.recovery_timeout}s). Transitioning OPEN -> HALF_OPEN (probing).")
                self.state = CircuitState.HALF_OPEN
                return True
            return False
        if self.state == CircuitState.HALF_OPEN:
            # Allow single probe request
            return True
        return False

class ResilientBookstoreClient:
    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        timeout: float = 2.0,
        max_retries: int = 3,
        backoff_factor: float = 0.5,
        circuit_breaker: Optional[CircuitBreaker] = None
    ):
        self.base_url = base_url
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.cb = circuit_breaker or CircuitBreaker(failure_threshold=3, recovery_timeout=4.0)
        self.session = requests.Session()

    def create_book(self, title: str, author: str, price: float, idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        """
        Creates a book using POST /books with:
        1. Circuit Breaker protection.
        2. Strict timeout (2.0s).
        3. Safe exponential backoff retries protected by Idempotency-Key.
        """
        if not self.cb.allow_request():
            remaining = max(0.0, self.cb.recovery_timeout - (time.time() - self.cb.last_state_change))
            raise CircuitBreakerOpenException(
                f"Circuit is OPEN. Call blocked locally to protect downstream service. Cooldown remaining: {remaining:.2f}s"
            )

        if not idempotency_key:
            idempotency_key = str(uuid.uuid4())

        payload = {"title": title, "author": author, "price": price}
        headers = {
            "Idempotency-Key": idempotency_key,
            "x-request-id": f"caller-{uuid.uuid4().hex[:8]}"
        }
        url = f"{self.base_url}/books"

        attempt = 0
        while attempt <= self.max_retries:
            attempt += 1
            try:
                logger.info(f"📤 [HTTP] Attempt {attempt}/{self.max_retries + 1} POST {url} (Key: {idempotency_key[:8]}...)")
                response = self.session.post(url, json=payload, headers=headers, timeout=self.timeout)

                # Successful creation or idempotent cached return
                if response.status_code in (200, 201):
                    self.cb.record_success()
                    logger.info(f"✅ [HTTP] Success ({response.status_code}) Cache-Lookup: {response.headers.get('X-Cache-Lookup', 'N/A')}")
                    return response.json()

                # Rate limited
                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After", "unknown")
                    logger.warning(f"🛑 [HTTP 429] Rate limited. Retry-After header: {retry_after}s.")
                    # Do not treat 429 as a circuit-breaker tripping failure, but do not retry immediately
                    return response.json()

                # Client-side 4xx errors (non-transient, e.g. 400, 409, 422) -> Do NOT retry
                if 400 <= response.status_code < 500:
                    logger.warning(f"⚠️ [HTTP {response.status_code}] Client error. Detail: {response.text}")
                    # Non-transient errors do not trip breaker
                    return response.json()

                # Server-side 5xx errors (transient) -> Retry with backoff
                if response.status_code >= 500:
                    logger.error(f"❌ [HTTP {response.status_code}] Server error: {response.text}")
                    if attempt > self.max_retries:
                        self.cb.record_failure()
                        return response.json()

            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as ex:
                logger.error(f"❌ [Network Exception] {type(ex).__name__}: {ex}")
                if attempt > self.max_retries:
                    self.cb.record_failure()
                    raise ex

            # Calculate exponential backoff
            sleep_time = self.backoff_factor * (2 ** (attempt - 1))
            logger.info(f"⏳ Transient error encountered. Backing off for {sleep_time:.2f}s before retry...")
            time.sleep(sleep_time)

        self.cb.record_failure()
        raise RuntimeError("Max retries exceeded without successful response.")

def run_demonstration():
    print("=" * 70)
    print("ASSIGNMENT 2 - PART A: RESILIENT HTTP CALLER DEMONSTRATION")
    print("Settings: Timeout=2.0s, Max Retries=3, Backoff=0.5s, Breaker Threshold=3, Breaker Recovery=4.0s")
    print("=" * 70)

    client = ResilientBookstoreClient(
        base_url="http://127.0.0.1:8000",
        timeout=2.0,
        max_retries=3,
        backoff_factor=0.5,
        circuit_breaker=CircuitBreaker(failure_threshold=3, recovery_timeout=4.0)
    )

    # 1. Normal Success and Idempotency Verification
    print("\n--- TEST 1: Normal Successful Request & Safe Repeat (Idempotency) ---")
    shared_key = f"demo-key-{uuid.uuid4().hex[:6]}"
    demo_book_title = f"Domain-Driven Design {uuid.uuid4().hex[:4]}"
    res1 = client.create_book(demo_book_title, "Eric Evans", 42.00, idempotency_key=shared_key)
    print(f"Initial Response: {res1}")

    print("\nRepeating the exact same request with identical Idempotency-Key...")
    res2 = client.create_book(demo_book_title, "Eric Evans", 42.00, idempotency_key=shared_key)
    print(f"Repeated Response (Safe Repeat): {res2}")

    # 2. Input Validation (RFC 7807)
    print("\n--- TEST 2: Input Validation Rejection (Negative Price) ---")
    res_invalid = client.create_book("Invalid Book", "Unknown", -15.00)
    print(f"Validation Error Response: {res_invalid}")

    # 3. Circuit Breaker: Trigger Consecutive Failures & Trip Breaker
    print("\n--- TEST 3: Tripping the Circuit Breaker via Downstream Server Outage ---")
    bad_client = ResilientBookstoreClient(
        base_url="http://127.0.0.1:9999", # Non-existent port to simulate server crash
        timeout=1.0,
        max_retries=1,
        backoff_factor=0.2,
        circuit_breaker=CircuitBreaker(failure_threshold=3, recovery_timeout=4.0)
    )

    for i in range(1, 4):
        print(f"\nTriggering failure #{i}...")
        try:
            bad_client.create_book("Ghost Book", "Ghost Author", 10.00)
        except Exception as e:
            print(f"Caught expected failure: {type(e).__name__}")

    # 4. Immediate Call Blocking (Fast Fail)
    print("\n--- TEST 4: Immediate Call Blocking (Circuit Breaker OPEN) ---")
    t0 = time.time()
    try:
        bad_client.create_book("Blocked Book", "Blocked Author", 10.00)
    except CircuitBreakerOpenException as cbe:
        elapsed = time.time() - t0
        print(f"BLOCKED! {cbe}")
        print(f"Call aborted locally in {elapsed*1000:.2f}ms (Fast-Fail, zero network overhead)")

    # 5. Circuit Recovery & Half-Open Transition
    print("\n--- TEST 5: Recovery Period and Transition to HALF-OPEN / CLOSED ---")
    print("Waiting 4.2 seconds for circuit recovery cooldown...")
    time.sleep(4.2)
    # Point breaker to the live working server
    bad_client.base_url = "http://127.0.0.1:8000"
    print("Sending probe request through HALF-OPEN circuit...")
    recovery_res = bad_client.create_book(f"Recovery Book {uuid.uuid4().hex[:4]}", "SRE Author", 19.99)
    print(f"Probe request succeeded! Response: {recovery_res}")
    print(f"Final Circuit State: {bad_client.cb.state.value}")

    # 6. Rate Limiting Demonstration
    print("\n--- TEST 6: Demonstrating Rate Limiting (HTTP 429 & Retry-After) ---")
    print("Rapidly firing 6 requests to trigger the 5 req/10s rate limit on POST /books...")
    for i in range(1, 7):
        r = requests.post(
            "http://127.0.0.1:8000/books",
            json={"title": f"Burst Book {uuid.uuid4().hex[:4]}", "author": "Spammer", "price": 9.99},
            headers={"x-request-id": f"burst-{i}"}
        )
        print(f"Request #{i} -> Status: {r.status_code}, Retry-After: {r.headers.get('Retry-After')}")

if __name__ == "__main__":
    run_demonstration()

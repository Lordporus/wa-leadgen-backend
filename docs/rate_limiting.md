# Rate Limiting Strategy

## Current Rate Limiter

**Storage:**
- `slowapi`
- In-memory storage backend
- Designed for our current single Render instance deployment

**Limitations:**
- Counters reset after every backend restart or redeploy.
- State is not shared across multiple instances (if we were to scale horizontally in the future).

**Future Migration:**
- We can easily migrate to Redis by simply updating the `slowapi` `Limiter` configuration with a Redis `storage_uri` (e.g., `storage_uri="redis://..."`).
- **No route code changes** will be required during this migration, as the `slowapi` decorators (`@limiter.limit`) abstract the storage backend entirely.

## Endpoint-Specific Tuning Observations (Auth APIs)

Currently, the Authenticated APIs (`/api/v1/*`) are rate-limited at **120 requests per minute** based on the `Client ID`.

**Important Note for Production:**
This threshold is a baseline. After observing real-world production traffic, we may need to adjust this limit. For instance, if dashboard usage creates bursty traffic patterns (e.g., loading many leads concurrently), we will need to tune this threshold upwards to prevent false positives for legitimate clients. Tuning can be done directly on the decorators in `main.py` without architectural changes.

import sys

with open('main.py', 'r') as f:
    content = f.read()

if 'from sqlalchemy import text' not in content:
    content = content.replace('from database import SessionLocal', 'from database import SessionLocal\nfrom sqlalchemy import text')

new_endpoints = """

@app.get("/api/analytics/funnel", dependencies=[Depends(require_api_key)])
def analytics_funnel(client: Client = Depends(require_api_key)):
    \"\"\"
    Returns a snapshot count of leads by status for the authenticated client.
    \"\"\"
    with SessionLocal() as s:
        query = text(\"\"\"
            SELECT status, COUNT(id) as count
            FROM leads
            WHERE client_id = :client_id
            GROUP BY status
        \"\"\")
        results = s.execute(query, {"client_id": client.id}).fetchall()
        
        return {row.status: row.count for row in results}

@app.get("/api/analytics/response-time", dependencies=[Depends(require_api_key)])
def analytics_response_time(client: Client = Depends(require_api_key)):
    \"\"\"
    Pairs each INBOUND message with the next OUTBOUND message to calculate response times.
    Uses Postgres window functions to determine the exact gap.
    \"\"\"
    with SessionLocal() as s:
        # Overall aggregates (Average, Median, Max)
        stats_query = text(\"\"\"
            WITH paired_messages AS (
                SELECT 
                    m.lead_id,
                    m.direction,
                    m.created_at as inbound_time,
                    LEAD(m.created_at) OVER (PARTITION BY m.lead_id ORDER BY m.created_at) as next_time,
                    LEAD(m.direction) OVER (PARTITION BY m.lead_id ORDER BY m.created_at) as next_direction
                FROM messages m
                JOIN leads l ON m.lead_id = l.id
                WHERE l.client_id = :client_id
            ),
            response_times AS (
                SELECT 
                    EXTRACT(EPOCH FROM (next_time - inbound_time)) as response_time_seconds
                FROM paired_messages
                WHERE direction = 'INBOUND' AND next_direction = 'OUTBOUND'
            )
            SELECT 
                COALESCE(AVG(response_time_seconds), 0) as avg_seconds,
                COALESCE(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY response_time_seconds), 0) as median_seconds,
                COALESCE(MAX(response_time_seconds), 0) as max_seconds
            FROM response_times
        \"\"\")
        
        stats = s.execute(stats_query, {"client_id": client.id}).fetchone()
        
        # Time-series (last 14 days)
        daily_query = text(\"\"\"
            WITH paired_messages AS (
                SELECT 
                    m.lead_id,
                    m.direction,
                    m.created_at as inbound_time,
                    LEAD(m.created_at) OVER (PARTITION BY m.lead_id ORDER BY m.created_at) as next_time,
                    LEAD(m.direction) OVER (PARTITION BY m.lead_id ORDER BY m.created_at) as next_direction
                FROM messages m
                JOIN leads l ON m.lead_id = l.id
                WHERE l.client_id = :client_id
                  AND m.created_at >= CURRENT_DATE - INTERVAL '14 days'
            ),
            response_times AS (
                SELECT 
                    DATE(inbound_time) as date,
                    EXTRACT(EPOCH FROM (next_time - inbound_time)) as response_time_seconds
                FROM paired_messages
                WHERE direction = 'INBOUND' AND next_direction = 'OUTBOUND'
            )
            SELECT 
                date,
                AVG(response_time_seconds) as avg_seconds
            FROM response_times
            GROUP BY date
            ORDER BY date
        \"\"\")
        
        daily_results = s.execute(daily_query, {"client_id": client.id}).fetchall()
        
        return {
            "avg_seconds": round(float(stats.avg_seconds), 2) if stats and stats.avg_seconds else 0,
            "median_seconds": round(float(stats.median_seconds), 2) if stats and stats.median_seconds else 0,
            "max_seconds": round(float(stats.max_seconds), 2) if stats and stats.max_seconds else 0,
            "daily": [
                {"date": str(row.date), "avg_seconds": round(float(row.avg_seconds), 2)}
                for row in daily_results
            ]
        }

@app.get("/api/analytics/bookings", dependencies=[Depends(require_api_key)])
def analytics_bookings(client: Client = Depends(require_api_key)):
    \"\"\"
    Counts bookings by looking at SYSTEM messages indicating a Calendly confirmation.
    Scoped to the last 30 days.
    \"\"\"
    with SessionLocal() as s:
        query = text(\"\"\"
            WITH booking_messages AS (
                SELECT m.lead_id, m.created_at
                FROM messages m
                JOIN leads l ON m.lead_id = l.id
                WHERE l.client_id = :client_id
                  AND m.direction = 'SYSTEM'
                  AND m.body ILIKE '%Calendly Booking Confirmed%'
                  AND m.created_at >= CURRENT_DATE - INTERVAL '30 days'
            )
            SELECT 
                DATE(created_at) as date,
                COUNT(lead_id) as count
            FROM booking_messages
            GROUP BY DATE(created_at)
            ORDER BY date
        \"\"\")
        
        daily_results = s.execute(query, {"client_id": client.id}).fetchall()
        
        total = sum(row.count for row in daily_results)
        
        return {
            "total_bookings": total,
            "daily": [
                {"date": str(row.date), "count": row.count}
                for row in daily_results
            ]
        }
"""

with open('main.py', 'w') as f:
    f.write(content + new_endpoints)
print('Done modifying main.py')

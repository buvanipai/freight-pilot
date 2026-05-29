-- db/schema.sql
CREATE TABLE IF NOT EXISTS carriers (
    dot TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    city TEXT,
    state TEXT,
    status TEXT DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS customers (
    id TEXT PRIMARY KEY,
    company_name TEXT NOT NULL,
    tier TEXT NOT NULL,
    industry TEXT NOT NULL,
    state TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shipments (
    id TEXT PRIMARY KEY,
    carrier_dot TEXT REFERENCES carriers(dot),
    customer_id TEXT REFERENCES customers(id),
    origin_state TEXT NOT NULL,
    destination_state TEXT NOT NULL,
    mode TEXT NOT NULL,
    commodity TEXT NOT NULL,
    status TEXT NOT NULL,
    carrier_status TEXT DEFAULT 'active',
    freight_value_usd REAL NOT NULL,
    weight_tons REAL NOT NULL,
    eta_hours REAL NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    last_update TIMESTAMPTZ NOT NULL,
    resolved BOOLEAN DEFAULT FALSE,
    resolved_at TIMESTAMPTZ,
    predicted_eta_hours REAL,
    anomaly_score REAL,
    is_anomaly BOOLEAN DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS events (
    id BIGSERIAL PRIMARY KEY,
    shipment_id TEXT REFERENCES shipments(id),
    event_type TEXT NOT NULL,
    mode TEXT NOT NULL,
    location TEXT,
    note TEXT,
    timestamp TIMESTAMPTZ NOT NULL,
    stream_id TEXT
);

CREATE TABLE IF NOT EXISTS pipeline_metrics (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL,
    events_processed INTEGER DEFAULT 0,
    events_per_second REAL DEFAULT 0,
    consumer_lag INTEGER DEFAULT 0,
    exceptions_detected INTEGER DEFAULT 0,
    anomalies_detected INTEGER DEFAULT 0,
    processing_latency_ms REAL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_shipments_status ON shipments(status);
CREATE INDEX IF NOT EXISTS idx_shipments_carrier ON shipments(carrier_dot);
CREATE INDEX IF NOT EXISTS idx_shipments_created ON shipments(created_at);
CREATE INDEX IF NOT EXISTS idx_events_shipment ON events(shipment_id);
CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp);

-- Conformal prediction columns
ALTER TABLE shipments ADD COLUMN IF NOT EXISTS eta_predicted REAL;
ALTER TABLE shipments ADD COLUMN IF NOT EXISTS eta_lower REAL;
ALTER TABLE shipments ADD COLUMN IF NOT EXISTS eta_upper REAL;
ALTER TABLE shipments ADD COLUMN IF NOT EXISTS eta_confidence REAL;

-- Exception resolution outcomes for feedback loop
CREATE TABLE IF NOT EXISTS exception_outcomes (
    id BIGSERIAL PRIMARY KEY,
    shipment_id TEXT REFERENCES shipments(id),
    resolved_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    actual_eta_hours REAL,
    resolution_type TEXT NOT NULL DEFAULT 'other',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_outcomes_shipment ON exception_outcomes(shipment_id);
CREATE INDEX IF NOT EXISTS idx_outcomes_created ON exception_outcomes(created_at);

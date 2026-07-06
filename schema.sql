-- ═══════════════════════════════════════════════════════════════
-- MASTER BUTLER BIDDING SYSTEM — DATABASE BLUEPRINT (PostgreSQL)
--
-- This file is the plan for what the Render Postgres database stores.
-- Apply it once with:  psql <connection-string> -f schema.sql
--
-- Design rules baked in:
--   * every bid keeps its full story (audit trail — nothing is ever
--     silently changed or deleted)
--   * bid statuses follow the lifecycle we agreed on
--   * auto-send is a COLUMN DEFAULTED OFF, not a code path someone
--     can accidentally enable
-- ═══════════════════════════════════════════════════════════════

-- ── CUSTOMERS ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS customers (
    id            SERIAL PRIMARY KEY,
    name          TEXT,
    email         TEXT,
    phone         TEXT,
    jobber_client_id TEXT,           -- link to Jobber once created there
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_customers_email ON customers (lower(email));

-- ── PROPERTIES ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS properties (
    id            SERIAL PRIMARY KEY,
    customer_id   INT REFERENCES customers(id),
    address       TEXT NOT NULL,
    address_normalized TEXT,          -- for duplicate matching
    latitude      DOUBLE PRECISION,
    longitude     DOUBLE PRECISION,
    sqft          INT,
    stories       TEXT,
    roof_material TEXT,
    pitch         TEXT,
    roofline_linear_ft INT,           -- captured for holiday lights later
    data_source   TEXT,               -- 'google_solar' / 'records' / 'office'
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_properties_addr ON properties (address_normalized);

-- ── REQUESTS (one incoming email/form = one request) ─────────
CREATE TABLE IF NOT EXISTS requests (
    id            SERIAL PRIMARY KEY,
    customer_id   INT REFERENCES customers(id),
    property_id   INT REFERENCES properties(id),
    source        TEXT NOT NULL,      -- 'email' / 'web_form' / 'phone'
    thread_id     TEXT,               -- email thread, for dedup
    raw_subject   TEXT,
    services_requested TEXT[],        -- parser output
    kind          TEXT NOT NULL,      -- 'new_request' / 'question' / 'scheduling'
    duplicate_of  INT REFERENCES requests(id),  -- linked, never dropped
    received_at   TIMESTAMPTZ NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── BIDS (the heart of the system) ───────────────────────────
CREATE TABLE IF NOT EXISTS bids (
    id            SERIAL PRIMARY KEY,
    request_id    INT REFERENCES requests(id),
    property_id   INT REFERENCES properties(id),
    status        TEXT NOT NULL DEFAULT 'pending_review'
                  CHECK (status IN ('pending_review','in_review','approved',
                                    'sent','accepted','declined','expired',
                                    'on_hold','archived')),
    confidence    INT,                -- 0-100 data-quality score
    total         NUMERIC(10,2),      -- summary; per-service detail in bid_lines
    office_notes  TEXT[],             -- the ⚠ flags shown to reviewers
    auto_send_allowed BOOLEAN NOT NULL DEFAULT FALSE,  -- HARD-LOCKED OFF
    reviewed_by   TEXT,
    sent_at       TIMESTAMPTZ,
    expires_at    TIMESTAMPTZ,        -- sent + 30 days (Tom to confirm window)
    jobber_quote_id TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_bids_status ON bids (status);

-- ── BID LINES (one row per service — the LEARNING RECORD) ────
-- This is where the three numbers live side by side for EVERY service:
--   system_price  = what the live engine proposed
--   shadow_price  = what the background learning engine WOULD propose
--                   (shadow mode — never affects the real bid)
--   final_price   = what the office approved
-- Plus the WHY, captured in the office workflow (NOT from customer email):
--   adjust_reason = one-tap category (what the machine learns from)
--   adjust_note   = optional one-line internal note (never sent to customer)
CREATE TABLE IF NOT EXISTS bid_lines (
    id            SERIAL PRIMARY KEY,
    bid_id        INT REFERENCES bids(id),
    service       TEXT NOT NULL,      -- 'gutter_cleaning','pw_driveway','moss_treatment',...
    measured_inputs JSONB,           -- {sqft, roof_sqft, area, rate_used, multipliers}
    system_price  NUMERIC(10,2),     -- live engine's proposal
    shadow_price  NUMERIC(10,2),     -- learning engine's silent proposal (shadow mode)
    final_price   NUMERIC(10,2),     -- office-approved (NULL until reviewed)
    adjust_reason TEXT CHECK (adjust_reason IN (
                    'too_low','too_high','heavy_buildup','hard_access',
                    'measurement_off','added_service','relationship_discount',
                    'other')),        -- internal only
    adjust_note   TEXT,               -- optional free text, INTERNAL, never to customer
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_bid_lines_service ON bid_lines (service);
-- ^ indexed by service so "show me every driveway: system vs shadow vs final vs why"
--   is one fast query, across the board, for any service.

-- ── AUDIT LOG (every decision, human or machine) ─────────────
CREATE TABLE IF NOT EXISTS audit_log (
    id            SERIAL PRIMARY KEY,
    bid_id        INT REFERENCES bids(id),
    actor         TEXT NOT NULL,      -- 'system' / 'laree' / 'dallon' / ...
    action        TEXT NOT NULL,      -- 'created' / 'price_adjusted' / 'approved' / ...
    detail        JSONB,              -- before/after values
    at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── PROMOTIONS (date-ranged, office-editable discounts) ──────
CREATE TABLE IF NOT EXISTS promotions (
    id            SERIAL PRIMARY KEY,
    name          TEXT NOT NULL,      -- 'September Lights 15%'
    service       TEXT,               -- NULL = applies to all
    percent_off   NUMERIC(5,2) NOT NULL,
    starts_on     DATE NOT NULL,
    ends_on       DATE NOT NULL,
    labor_only    BOOLEAN NOT NULL DEFAULT FALSE,
    active        BOOLEAN NOT NULL DEFAULT TRUE
);

-- The real 2026 promos from the Jobber catalog, ready to toggle:
INSERT INTO promotions (name, service, percent_off, starts_on, ends_on, labor_only)
VALUES
  ('Holiday Lights early install — September', 'holiday_lights', 15, '2026-09-01', '2026-09-30', TRUE),
  ('Holiday Lights early install — October',   'holiday_lights', 10, '2026-10-01', '2026-10-31', TRUE),
  ('Holiday Lights — December',                'holiday_lights', 10, '2026-12-01', '2026-12-31', TRUE)
ON CONFLICT DO NOTHING;

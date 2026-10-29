CREATE TABLE products (
    id SERIAL PRIMARY KEY,
    product_code VARCHAR(120) NOT NULL UNIQUE,
    name VARCHAR(500) NOT NULL,
    brand VARCHAR(200) NOT NULL,
    family VARCHAR(200),
    mrp NUMERIC(14,2),
    pack_size VARCHAR(120),
    selling_rate NUMERIC(27,15),
    stock NUMERIC(14,3),
    active BOOLEAN NOT NULL DEFAULT TRUE,
    source_details JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX ix_products_product_code ON products(product_code);
CREATE INDEX ix_products_brand ON products(brand);

CREATE TABLE parties (
    id SERIAL PRIMARY KEY,
    party_code VARCHAR(120) NOT NULL UNIQUE,
    name VARCHAR(300) NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE INDEX ix_parties_party_code ON parties(party_code);

CREATE TABLE salespersons (
    id SERIAL PRIMARY KEY,
    telegram_user_id BIGINT NOT NULL UNIQUE,
    display_name VARCHAR(200) NOT NULL,
    active BOOLEAN NOT NULL DEFAULT FALSE,
    allow_discount BOOLEAN NOT NULL DEFAULT FALSE,
    allow_free_quantity BOOLEAN NOT NULL DEFAULT FALSE,
    max_discount_percent NUMERIC(5,2) NOT NULL DEFAULT 0
);
CREATE INDEX ix_salespersons_telegram_user_id ON salespersons(telegram_user_id);

CREATE TABLE salesperson_brands (
    id SERIAL PRIMARY KEY,
    salesperson_id INTEGER NOT NULL REFERENCES salespersons(id) ON DELETE CASCADE,
    brand VARCHAR(200) NOT NULL,
    CONSTRAINT uq_salesperson_brand UNIQUE (salesperson_id, brand)
);
CREATE INDEX ix_salesperson_brands_brand ON salesperson_brands(brand);

CREATE TABLE salesperson_parties (
    id SERIAL PRIMARY KEY,
    salesperson_id INTEGER NOT NULL REFERENCES salespersons(id) ON DELETE CASCADE,
    party_id INTEGER NOT NULL REFERENCES parties(id) ON DELETE CASCADE,
    CONSTRAINT uq_salesperson_party UNIQUE (salesperson_id, party_id)
);

CREATE TABLE bot_sessions (
    id SERIAL PRIMARY KEY,
    telegram_user_id BIGINT NOT NULL UNIQUE,
    state JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX ix_bot_sessions_telegram_user_id ON bot_sessions(telegram_user_id);

CREATE TABLE orders (
    id SERIAL PRIMARY KEY,
    order_number VARCHAR(40) NOT NULL UNIQUE,
    submission_key VARCHAR(80) UNIQUE,
    salesperson_id INTEGER NOT NULL REFERENCES salespersons(id),
    party_id INTEGER NOT NULL REFERENCES parties(id),
    brand VARCHAR(200) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'Draft'
        CHECK (status IN ('Draft', 'Submitted', 'Approved', 'Rejected', 'Exported')),
    remarks TEXT,
    draft_data JSONB NOT NULL DEFAULT '{}'::jsonb,
    submitted_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX ix_orders_order_number ON orders(order_number);
CREATE INDEX ix_orders_status ON orders(status);

CREATE TABLE order_items (
    id SERIAL PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products(id),
    product_code VARCHAR(120) NOT NULL,
    product_name VARCHAR(500) NOT NULL,
    brand VARCHAR(200) NOT NULL,
    pack_size VARCHAR(120),
    quantity NUMERIC(12,3) NOT NULL CHECK (quantity > 0),
    free_quantity NUMERIC(12,3) NOT NULL DEFAULT 0 CHECK (free_quantity >= 0),
    rate NUMERIC(27,15) NOT NULL CHECK (rate >= 0),
    discount_percent NUMERIC(5,2) NOT NULL DEFAULT 0 CHECK (discount_percent >= 0 AND discount_percent <= 100),
    line_total NUMERIC(14,2) NOT NULL
);
CREATE INDEX ix_order_items_order_id ON order_items(order_id);

CREATE TABLE order_audit (
    id SERIAL PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    actor VARCHAR(200) NOT NULL,
    action VARCHAR(100) NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX ix_order_audit_order_id ON order_audit(order_id);

CREATE TABLE import_batches (
    id SERIAL PRIMARY KEY,
    kind VARCHAR(30) NOT NULL,
    filename VARCHAR(255) NOT NULL,
    summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    errors JSONB NOT NULL DEFAULT '[]'::jsonb,
    successful_rows JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE marg_mappings (
    id INTEGER PRIMARY KEY,
    columns JSONB NOT NULL,
    required_fields JSONB NOT NULL,
    combined_allowed BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE scanner_runs (
    id SERIAL PRIMARY KEY,
    telegram_user_id BIGINT NOT NULL,
    filename VARCHAR(255) NOT NULL,
    result JSONB NOT NULL DEFAULT '{}'::jsonb,
    document_bytes BYTEA,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX ix_scanner_runs_telegram_user_id ON scanner_runs(telegram_user_id);
CREATE INDEX ix_scanner_runs_expires_at ON scanner_runs(expires_at);

CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS analytics;

CREATE TABLE IF NOT EXISTS core.report (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    venue text NOT NULL,
    period_start date NOT NULL,
    period_end date NOT NULL,
    days_covered integer NOT NULL,
    filename text NOT NULL,
    content_sha256 text NOT NULL,
    parser_version text NOT NULL,
    ingested_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT report_period_unique UNIQUE (venue, period_start, period_end),
    CONSTRAINT report_period_order CHECK (period_end >= period_start),
    CONSTRAINT report_days_covered
        CHECK (days_covered = (period_end - period_start) + 1)
);

CREATE TABLE IF NOT EXISTS core.sales_row (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    report_id bigint NOT NULL REFERENCES core.report (id) ON DELETE CASCADE,
    venue text NOT NULL,
    period_start date NOT NULL,
    period_end date NOT NULL,
    item_name text NOT NULL,
    item_key text NOT NULL,
    sales_category text NOT NULL,
    menu_group text NOT NULL,
    units numeric(14, 2) NOT NULL,
    gross_sales numeric(14, 2) NOT NULL,
    item_discounts numeric(14, 2) NOT NULL,
    bill_discounts numeric(14, 2) NOT NULL,
    net_sales numeric(14, 2) NOT NULL,
    CONSTRAINT sales_row_natural_key
        UNIQUE (report_id, item_name, sales_category, menu_group),
    CONSTRAINT sales_row_arithmetic
        CHECK (gross_sales - item_discounts - bill_discounts = net_sales)
);

CREATE INDEX IF NOT EXISTS sales_row_period_idx
    ON core.sales_row (venue, period_start);
CREATE INDEX IF NOT EXISTS sales_row_item_key_idx
    ON core.sales_row (item_key);
CREATE INDEX IF NOT EXISTS sales_row_category_idx
    ON core.sales_row (sales_category);

CREATE TABLE IF NOT EXISTS core.report_summary (
    report_id bigint PRIMARY KEY REFERENCES core.report (id) ON DELETE CASCADE,
    label text NOT NULL,
    units numeric(14, 2) NOT NULL,
    gross_sales numeric(14, 2) NOT NULL,
    item_discounts numeric(14, 2) NOT NULL,
    bill_discounts numeric(14, 2) NOT NULL,
    net_sales numeric(14, 2) NOT NULL
);

CREATE TABLE IF NOT EXISTS core.ingest_log (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    venue text,
    period_start date,
    period_end date,
    filename text NOT NULL,
    content_sha256 text,
    parser_version text,
    status text NOT NULL
        CHECK (status IN ('loaded', 'replaced', 'skipped', 'failed')),
    message text,
    ingested_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ingest_log_period_idx
    ON core.ingest_log (venue, period_start, ingested_at DESC);

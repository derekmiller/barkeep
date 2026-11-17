CREATE OR REPLACE VIEW analytics.v_sales_current AS
SELECT
    r.venue,
    r.period_start,
    r.period_end,
    r.days_covered,
    s.item_name,
    s.item_key,
    s.sales_category,
    s.menu_group AS menu_group_raw,
    regexp_replace(s.menu_group, '[[:space:]]+', ' ', 'g') AS menu_group,
    s.units,
    s.gross_sales,
    s.item_discounts,
    s.bill_discounts,
    s.net_sales
FROM core.sales_row AS s
JOIN core.report AS r ON r.id = s.report_id;

CREATE OR REPLACE VIEW analytics.v_weekly_totals AS
SELECT
    venue,
    period_start,
    period_end,
    SUM(units) AS units,
    SUM(gross_sales) AS gross_sales,
    SUM(item_discounts) AS item_discounts,
    SUM(bill_discounts) AS bill_discounts,
    SUM(net_sales) AS net_sales,
    COUNT(*) AS item_rows
FROM analytics.v_sales_current
GROUP BY venue, period_start, period_end;

CREATE OR REPLACE VIEW analytics.v_weekly_item AS
SELECT
    venue,
    period_start,
    period_end,
    item_key,
    item_name,
    sales_category,
    SUM(units) AS units,
    SUM(gross_sales) AS gross_sales,
    SUM(item_discounts) AS item_discounts,
    SUM(bill_discounts) AS bill_discounts,
    SUM(net_sales) AS net_sales
FROM analytics.v_sales_current
GROUP BY venue, period_start, period_end, item_key, item_name, sales_category;

CREATE OR REPLACE VIEW analytics.v_weekly_category AS
SELECT
    venue,
    period_start,
    period_end,
    sales_category,
    SUM(units) AS units,
    SUM(gross_sales) AS gross_sales,
    SUM(item_discounts) AS item_discounts,
    SUM(bill_discounts) AS bill_discounts,
    SUM(net_sales) AS net_sales
FROM analytics.v_sales_current
GROUP BY venue, period_start, period_end, sales_category;

CREATE OR REPLACE VIEW analytics.v_item_catalog AS
SELECT DISTINCT
    venue,
    item_key,
    item_name,
    sales_category
FROM analytics.v_sales_current;

COMMENT ON VIEW analytics.v_sales_current IS
    'Current weekly sales rows. One row per item/category/menu_group per report.';
COMMENT ON VIEW analytics.v_weekly_totals IS
    'Weekly report totals, suitable for trend charts.';
COMMENT ON VIEW analytics.v_weekly_item IS
    'Weekly item totals aggregated across menu groups.';
COMMENT ON VIEW analytics.v_weekly_category IS
    'Weekly sales-category totals.';
COMMENT ON VIEW analytics.v_item_catalog IS
    'Distinct item display names and keys for search and filters.';
COMMENT ON COLUMN analytics.v_sales_current.menu_group_raw IS
    'Raw menu group, including embedded newlines.';
COMMENT ON COLUMN analytics.v_sales_current.menu_group IS
    'Menu group with whitespace runs collapsed to single spaces, for display.';
COMMENT ON COLUMN analytics.v_sales_current.units IS
    'Gross units sold. Refunds are not subtracted.';
COMMENT ON COLUMN analytics.v_sales_current.net_sales IS
    'Revenue after item and bill discounts. Not a unit count.';

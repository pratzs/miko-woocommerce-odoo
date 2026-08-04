# WooCommerce Odoo Connector: Orders, Stock, Products (Miko)

Two-way sync between a WooCommerce store and Odoo, on a schedule. Built on the
WooCommerce REST API v3.

Requires the free **E-Commerce Connector Engine (Miko)** (`miko_ecommerce_core`),
which Odoo installs automatically.

| | |
|---|---|
| Module | `miko_woocommerce` |
| Series | 16.0, 17.0, 18.0, 19.0 |
| Licence | OPL-1 |
| Price | USD 399 |
| Tests | 57, all four series |

## What it refuses to guess

- **Never imports the same order twice.** Identity goes through the mapping table
  before anything is written.
- **Never drops a tax.** Anything WooCommerce sends with no Odoo equivalent stops
  and asks rather than producing an invoice quietly short by the tax.
- **Never attaches a sale to a nearly-matching variation.** Exact option-set
  equality only.
- **Never imports a total that is not what the customer paid.** Checked against
  WooCommerce's own figure; anything that disagrees stays a quotation, flagged.
- **Never sends stock to an untracked product.** Woo ignores `stock_quantity`
  when `manage_stock` is off, so sending it would report success and change
  nothing.
- **Never talks to a store over plain HTTP.** The consumer key and secret go with
  every request.

## Marking an order shipped

WooCommerce has no fulfilment object. An order is shipped by moving its status,
and tracking numbers live in `meta_data` under whichever key the shipping plugin
uses — so that key is a setting, not a guess. Left empty, the status still moves
and no tracking is written anywhere misleading.

## Testing

```bash
python3 _dev/build_versions.py && ../_odoo-portfolio/certify.sh miko-woocommerce-odoo miko_woocommerce 45 miko-ecommerce-core-odoo
```

Nothing in the suite touches the network.

## Support

support@tripsterdevelopers.com

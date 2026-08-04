# -*- coding: utf-8 -*-
{
    'name': 'WooCommerce Odoo Connector: Orders, Stock, Products (Miko)',
    'version': '16.0.1.0.0',
    'summary': 'Two-way WooCommerce Odoo sync on a schedule: import orders, '
               'products and customers, publish stock and order status',
    'description': """
Connect a WooCommerce store to Odoo and keep both sides in step, on a schedule.

Built on the WooCommerce REST API v3, against the merchant's own WordPress, which
is why it is careful about the things a hosted API never does to you: an HTML
error page from a caching plugin, a firewall answering instead of the API, or a
site that is simply slow.

What it will not do matters as much as what it will. It never imports the same
order twice, whatever happens mid-sync. It never invents a tax: anything
WooCommerce sends without an Odoo equivalent stops and asks rather than quietly
producing an invoice short by the tax. It never attaches a sale to a
nearly-matching variation. And it checks every imported order against the total
WooCommerce actually charged, leaving anything that disagrees as a quotation with
the difference spelled out.
""",
    'author': 'Tripster Developers',
    'website': 'https://tripsterdevelopers.com/odoo/',
    'category': 'eCommerce',
    'license': 'OPL-1',
    'depends': ['miko_ecommerce_core', 'sale_management', 'stock', 'account'],
    'external_dependencies': {'python': ['requests']},
    'data': [
        'security/ir.model.access.csv',
        'data/ir_cron.xml',
        'views/miko_woocommerce_views.xml',
    ],
    'images': ['images/banner.gif', 'images/banner.png'],
    'price': 399.00,
    'currency': 'USD',
    'application': False,
    'installable': True,
    'support': 'support@tripsterdevelopers.com',
}

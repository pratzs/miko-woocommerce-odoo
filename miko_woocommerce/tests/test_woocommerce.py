# -*- coding: utf-8 -*-
"""Tests for the WooCommerce connector.

Nothing here touches the network. The HTTP client is driven against a stubbed
transport and the import code against recorded payloads, so the suite gives the
same answer offline as it does in CI.

The tests are written around the ways a connector loses money or duplicates
data, because those are the failures that matter: importing twice, attaching a
sale to the wrong variation, dropping a tax, and importing a total that is not
what the customer paid.
"""
import json

from unittest.mock import patch

from odoo import fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import TransactionCase

from ..models.woo_client import WooClient, WooError, redact


class FakeResponse(object):
    def __init__(self, status_code=200, body=None, headers=None, text=None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}
        self.text = text if text is not None else json.dumps(body if body is not None else {})

    def json(self):
        if self._body is None:
            raise ValueError('not json')
        return self._body


class FakeClient(object):
    """Answers by looking at the method and path it was given."""

    def __init__(self, pages=None, records=None, fail=None):
        self.calls = []
        self.pages = pages or {}
        self.records = records or {}
        self.fail = fail

    def call(self, method, path, params=None, payload=None):
        self.calls.append((method, path, params, payload))
        if self.fail:
            raise WooError(self.fail)
        return self.records.get(path, {'id': 999}), {}

    def paginate(self, path, params=None, max_pages=2000):
        for record in self.pages.get(path, []):
            yield record


class WooCase(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.channel = self.env['miko.ecommerce.channel'].create({
            'name': 'Northwind Woo',
            'platform': 'woocommerce',
            'woo_url': 'https://shop.example.com',
            'company_id': self.company.id,
        })
        self.channel.sudo().write({'woo_key': 'ck_test', 'woo_secret': 'cs_test'})
        self.client = FakeClient()

    def _with_client(self, client=None):
        return patch.object(type(self.channel), '_woo_client',
                            return_value=client or self.client)

    def _tax_group(self):
        Group = self.env['account.tax.group']
        domain = [('company_id', '=', self.company.id)] if 'company_id' in Group._fields else []
        group = Group.search(domain, limit=1)
        if group:
            return group
        values = {'name': 'Miko Woo Test Group'}
        if 'company_id' in Group._fields:
            values['company_id'] = self.company.id
        if 'country_id' in Group._fields:
            values['country_id'] = self._country().id
        return Group.create(values)

    def _country(self):
        return (self.company.account_fiscal_country_id or self.company.country_id
                or self.env['res.country'].search([('code', '=', 'NZ')], limit=1))

    def _tax(self, name, amount):
        """country_id and tax_group_id are both NOT NULL on account_tax."""
        return self.env['account.tax'].create({
            'name': name, 'amount': amount, 'amount_type': 'percent',
            'type_tax_use': 'sale', 'company_id': self.company.id,
            'country_id': self._country().id,
            'tax_group_id': self._tax_group().id})


class TestClient(WooCase):

    def test_plain_http_is_refused_outright(self):
        """Basic auth over HTTP puts the consumer secret on the wire in clear."""
        with self.assertRaises(WooError) as caught:
            WooClient('http://shop.example.com', 'ck_x', 'cs_y')
        self.assertIn('https', str(caught.exception))

    def test_a_secret_is_never_written_into_an_error(self):
        leaked = "auth failed for ck_abc123DEF and cs_xyz789GHI"
        out = redact(leaked)
        self.assertNotIn('abc123DEF', out)
        self.assertNotIn('xyz789GHI', out)
        self.assertIn('ck_***', out)

    def test_missing_credentials_are_refused_before_any_request(self):
        with self.assertRaises(WooError):
            WooClient('https://shop.example.com', '', '')

    def test_html_instead_of_json_names_the_likely_cause(self):
        """WordPress, a caching plugin or a firewall answering instead of the API."""
        client = WooClient('https://shop.example.com', 'ck_x', 'cs_y')
        with patch.object(client._session, 'request',
                          return_value=FakeResponse(200, None, text='<html>oops</html>')):
            with self.assertRaises(WooError) as caught:
                client.call('GET', 'products')
        message = str(caught.exception)
        self.assertIn('not JSON', message)
        self.assertIn('wp-json', message)

    def test_bad_credentials_say_what_to_check(self):
        client = WooClient('https://shop.example.com', 'ck_x', 'cs_y')
        with patch.object(client._session, 'request', return_value=FakeResponse(401, {})):
            with self.assertRaises(WooError) as caught:
                client.call('GET', 'products')
        self.assertIn('Read/Write', str(caught.exception))

    def test_a_404_points_at_permalinks(self):
        client = WooClient('https://shop.example.com', 'ck_x', 'cs_y')
        with patch.object(client._session, 'request', return_value=FakeResponse(404, {})):
            with self.assertRaises(WooError) as caught:
                client.call('GET', 'products')
        self.assertIn('permalink', str(caught.exception).lower())

    def test_a_429_is_retried_then_succeeds(self):
        client = WooClient('https://shop.example.com', 'ck_x', 'cs_y')
        responses = [FakeResponse(429, {}, headers={'Retry-After': '0'}),
                     FakeResponse(200, [{'id': 1}])]
        with patch.object(client._session, 'request', side_effect=responses), \
                patch('odoo.addons.miko_woocommerce.models.woo_client.time.sleep'):
            body, _h = client.call('GET', 'products')
        self.assertEqual(body, [{'id': 1}])

    def test_a_woo_error_body_is_not_treated_as_a_record(self):
        """Woo reports failures as a JSON object with code and message."""
        client = WooClient('https://shop.example.com', 'ck_x', 'cs_y')
        body = {'code': 'woocommerce_rest_cannot_view', 'message': 'Sorry'}
        with patch.object(client._session, 'request', return_value=FakeResponse(200, body)):
            with self.assertRaises(WooError) as caught:
                client.call('GET', 'orders')
        self.assertIn('Read/Write', str(caught.exception))

    def test_pagination_stops_at_the_reported_total(self):
        client = WooClient('https://shop.example.com', 'ck_x', 'cs_y')
        pages = [FakeResponse(200, [{'id': 1}], headers={'X-WP-TotalPages': '2'}),
                 FakeResponse(200, [{'id': 2}], headers={'X-WP-TotalPages': '2'})]
        with patch.object(client._session, 'request', side_effect=pages):
            got = list(client.paginate('products'))
        self.assertEqual([r['id'] for r in got], [1, 2])

    def test_pagination_orders_by_id_so_records_cannot_shift_between_pages(self):
        client = WooClient('https://shop.example.com', 'ck_x', 'cs_y')
        seen = {}

        def capture(method, url, params=None, **kw):
            seen.update(params or {})
            return FakeResponse(200, [], headers={'X-WP-TotalPages': '1'})

        with patch.object(client._session, 'request', side_effect=capture):
            list(client.paginate('products'))
        self.assertEqual(seen.get('orderby'), 'id')
        self.assertEqual(seen.get('order'), 'asc')


class TestChannel(WooCase):

    def test_an_http_address_is_refused(self):
        with self.assertRaises(ValidationError):
            self.channel.woo_url = 'http://shop.example.com'

    def test_a_wp_json_address_is_refused(self):
        with self.assertRaises(ValidationError):
            self.channel.woo_url = 'https://shop.example.com/wp-json/wc/v3'

    def _draft(self, typed):
        """An onchange runs on a draft; a saved write fires the constraint first."""
        return self.env['miko.ecommerce.channel'].new({
            'name': 'Draft', 'platform': 'woocommerce', 'woo_url': typed})

    def test_a_pasted_admin_url_is_repaired(self):
        draft = self._draft('https://shop.example.com/wp-admin/admin.php?page=wc')
        draft._onchange_woo_url()
        self.assertEqual(draft.woo_url, 'https://shop.example.com')

    def test_http_is_upgraded_rather_than_rejected_while_typing(self):
        draft = self._draft('http://shop.example.com/')
        draft._onchange_woo_url()
        self.assertEqual(draft.woo_url, 'https://shop.example.com')

    def test_a_failure_is_reported_not_raised(self):
        """One broken store must not stop the others being tested."""
        with self._with_client(FakeClient(fail='nope')):
            with patch.object(type(self.channel), '_woo_client') as m:
                m.return_value.system_status.side_effect = WooError('nope')
                self.channel.action_test_connection()
        self.assertEqual(self.channel.connection_state, 'error')


class TestDirections(WooCase):

    def test_pushing_products_is_refused_when_the_store_pulls(self):
        self.channel.product_direction = 'in'
        with self.assertRaises(UserError):
            self.channel._export_woo_products()

    def test_pulling_products_is_refused_when_the_store_pushes(self):
        self.channel.product_direction = 'out'
        with self.assertRaises(UserError):
            self.channel._import_woo_products()

    def test_both_ways_allows_either(self):
        self.channel.product_direction = 'both'
        self.assertTrue(self.channel._require_direction('product_direction', 'in', 'P'))
        self.assertTrue(self.channel._require_direction('product_direction', 'out', 'P'))


class TestProducts(WooCase):

    def _simple(self, wid=100, sku='SIMPLE-1'):
        return {'id': wid, 'name': 'Alpine Wool Throw', 'type': 'simple',
                'slug': 'alpine-wool-throw', 'sku': sku, 'regular_price': '89.00',
                'manage_stock': True, 'attributes': []}

    def test_a_simple_product_imports_once_and_stays_one_product(self):
        node = self._simple()
        with self._with_client():
            template = self.channel._job_import_product(node)
            again = self.channel._job_import_product(node)
        self.assertEqual(template, again, 're-running must not create a second product')
        self.assertEqual(self.env['product.template'].search_count(
            [('name', '=', 'Alpine Wool Throw')]), 1)

    def test_a_simple_product_links_its_variant_and_carries_the_sku(self):
        with self._with_client():
            self.channel._job_import_product(self._simple())
        product = self.env['miko.ecommerce.mapping'].find_odoo_record(
            self.channel, 'product.product', 100)
        self.assertTrue(product)
        self.assertEqual(product.default_code, 'SIMPLE-1')
        self.assertTrue(product.woo_manage_stock)

    def test_a_variation_is_matched_on_its_exact_option_values(self):
        node = {'id': 200, 'name': 'Test Shirt', 'type': 'variable', 'slug': 'shirt',
                'attributes': [{'name': 'Colour', 'variation': True,
                                'options': ['Blue', 'Red']}]}
        client = FakeClient(pages={'products/200/variations': [
            {'id': 201, 'sku': 'BLUE', 'regular_price': '25.00', 'manage_stock': True,
             'attributes': [{'name': 'Colour', 'option': 'Blue'}]}]})
        with self._with_client(client):
            self.channel._job_import_product(node)
        product = self.env['miko.ecommerce.mapping'].find_odoo_record(
            self.channel, 'product.product', 201)
        self.assertTrue(product)
        self.assertEqual({v.name for v in product.product_template_attribute_value_ids},
                         {'Blue'})

    def test_a_variation_with_no_odoo_equivalent_is_left_unlinked(self):
        """Never attach a sale to a nearly-matching variation."""
        node = {'id': 300, 'name': 'Test Hat', 'type': 'variable', 'slug': 'hat',
                'attributes': [{'name': 'Colour', 'variation': True,
                                'options': ['Blue']}]}
        client = FakeClient(pages={'products/300/variations': [
            {'id': 301, 'sku': 'GREEN', 'manage_stock': True,
             'attributes': [{'name': 'Colour', 'option': 'Green'}]}]})
        with self._with_client(client):
            self.channel._job_import_product(node)
        self.assertFalse(self.env['miko.ecommerce.mapping'].find_odoo_record(
            self.channel, 'product.product', 301))

    def test_a_virtual_product_becomes_a_service(self):
        node = dict(self._simple(wid=400, sku='VIRT-1'), virtual=True)
        with self._with_client():
            template = self.channel._job_import_product(node)
        self.assertEqual(template.type, 'service')


class TestCustomers(WooCase):

    def _customer(self, wid=500, email='ada@example.com'):
        return {'id': wid, 'first_name': 'Ada', 'last_name': 'Lovelace',
                'email': email,
                'billing': {'address_1': '1 Queen Street', 'city': 'Auckland',
                            'postcode': '1010', 'country': 'NZ', 'phone': '+6421000000'}}

    def test_an_existing_contact_is_reused_on_an_exact_email_match(self):
        existing = self.env['res.partner'].create({'name': 'Ada L',
                                                   'email': 'ada@example.com'})
        partner = self.channel._upsert_woo_customer(self._customer())
        self.assertEqual(partner, existing)

    def test_a_value_typed_in_odoo_is_never_overwritten(self):
        existing = self.env['res.partner'].create(
            {'name': 'Ada L', 'email': 'ada@example.com', 'street': '99 Corrected Rd'})
        partner = self.channel._upsert_woo_customer(self._customer())
        self.assertEqual(partner.street, '99 Corrected Rd')

    def test_the_country_is_resolved_from_the_iso_code(self):
        partner = self.channel._upsert_woo_customer(self._customer(wid=501,
                                                                   email='b@example.com'))
        self.assertEqual(partner.country_id.code, 'NZ')

    def test_a_customer_with_no_name_still_gets_a_findable_one(self):
        node = self._customer(wid=502, email='noname@example.com')
        node['first_name'] = node['last_name'] = ''
        partner = self.channel._upsert_woo_customer(node)
        self.assertEqual(partner.name, 'noname@example.com')

    def test_importing_the_same_customer_twice_creates_one_contact(self):
        node = self._customer(wid=503, email='twice@example.com')
        self.assertEqual(self.channel._upsert_woo_customer(node),
                         self.channel._upsert_woo_customer(node))

    def test_a_customer_without_an_email_is_refused_on_export(self):
        self.channel.customer_direction = 'out'
        partner = self.env['res.partner'].create({'name': 'No Email',
                                                  'customer_rank': 1})
        with self._with_client():
            with self.assertRaises(UserError) as caught:
                self.channel._job_export_customer({'partner_id': partner.id})
        self.assertIn('email', str(caught.exception).lower())


class TestOrders(WooCase):

    def setUp(self):
        super().setUp()
        self.product = self.env['product.product'].create(
            {'name': 'Imported Widget', 'default_code': 'WIDGET-1',
             'type': 'consu', 'list_price': 100.0})
        self.channel.default_customer_id = self.env['res.partner'].create(
            {'name': 'Woo guest'})

    def _order(self, wid=700, total='100.00', taxes=None, tax_lines=None,
               subtotal='100.00', line_total='100.00'):
        return {
            'id': wid, 'number': str(wid), 'status': 'processing',
            'date_created': '2026-02-01T10:00:00', 'total': total,
            'customer_id': 0, 'billing': {'email': 'buyer@example.com',
                                          'first_name': 'Bo', 'last_name': 'Yer'},
            'tax_lines': tax_lines or [],
            'line_items': [{'id': 1, 'name': 'Imported Widget', 'product_id': 900,
                            'variation_id': 0, 'quantity': 1, 'sku': 'WIDGET-1',
                            'subtotal': subtotal, 'total': line_total,
                            'taxes': taxes or []}],
            'shipping_lines': [], 'fee_lines': [],
        }

    def test_an_order_imports_with_the_right_line_and_total(self):
        with self._with_client():
            order = self.channel._import_one_woo_order(self._order())
        self.assertEqual(order.order_line.product_id, self.product)
        self.assertEqual(order.amount_total, 100.0)
        self.assertTrue(order.woo_total_matches)

    def test_importing_the_same_order_twice_creates_one_order(self):
        """The failure that costs days to unpick."""
        node = self._order(wid=701)
        with self._with_client():
            first = self.channel._job_import_order(node)
            again = self.channel._job_import_order(node)
        self.assertEqual(first, again)
        self.assertEqual(self.env['sale.order'].search_count(
            [('woo_order_number', '=', '701')]), 1)

    def test_a_line_discount_is_kept_as_a_discount(self):
        with self._with_client():
            order = self.channel._import_one_woo_order(
                self._order(wid=702, total='80.00', subtotal='100.00',
                            line_total='80.00'))
        self.assertEqual(order.order_line.price_unit, 100.0)
        self.assertAlmostEqual(order.order_line.discount, 20.0, places=4)

    def test_a_total_that_disagrees_is_flagged_and_left_as_a_quotation(self):
        with self._with_client():
            order = self.channel._import_one_woo_order(
                self._order(wid=703, total='150.00'))
        self.assertFalse(order.woo_total_matches)
        self.assertEqual(order.state, 'draft')

    def test_a_flagged_order_is_never_auto_confirmed(self):
        self.channel.auto_confirm_orders = True
        with self._with_client():
            order = self.channel._import_one_woo_order(
                self._order(wid=704, total='150.00'))
        self.assertEqual(order.state, 'draft')

    def test_an_unmapped_tax_stops_the_import_by_default(self):
        """Better a refused order than an invoice short by the tax."""
        self.assertEqual(self.channel.unmapped_tax_policy, 'block')
        # 7.5% deliberately: a rate the demo chart of accounts also uses gets
        # auto-suggested by the mapping model, so the import would not block and
        # the test would prove nothing.
        node = self._order(wid=705, total='107.50',
                           taxes=[{'id': 9, 'total': '7.50'}],
                           tax_lines=[{'rate_id': 9, 'label': 'Import Duty',
                                       'rate_percent': 7.5}])
        with self._with_client():
            with self.assertRaises(UserError) as caught:
                self.channel._import_one_woo_order(node)
        self.assertIn('Import Duty', str(caught.exception))

    def test_a_mapped_tax_is_applied_to_the_line(self):
        tax = self._tax('Woo GST 15', 15.0)
        self.env['miko.ecommerce.tax'].create({
            'channel_id': self.channel.id, 'title': 'GST', 'rate': 0.15,
            'tax_id': tax.id})
        node = self._order(wid=706, total='115.00',
                           taxes=[{'id': 7, 'total': '15.00'}],
                           tax_lines=[{'rate_id': 7, 'label': 'GST',
                                       'rate_percent': 15}])
        with self._with_client():
            order = self.channel._import_one_woo_order(node)
        field = self.channel._sol_tax_field()
        self.assertIn(tax, order.order_line[field])
        self.assertAlmostEqual(order.amount_total, 115.0, places=2)

    def test_ignoring_unmapped_taxes_is_possible_but_never_the_default(self):
        self.channel.unmapped_tax_policy = 'ignore'
        node = self._order(wid=707, total='100.00',
                           taxes=[{'id': 7, 'total': '15.00'}],
                           tax_lines=[{'rate_id': 7, 'label': 'Duty',
                                       'rate_percent': 5}])
        with self._with_client():
            self.assertTrue(self.channel._import_one_woo_order(node))

    def test_a_guest_with_an_email_becomes_a_contact(self):
        with self._with_client():
            order = self.channel._import_one_woo_order(self._order(wid=708))
        self.assertEqual(order.partner_id.email, 'buyer@example.com')

    def test_a_guest_with_no_email_and_no_fallback_says_what_to_set(self):
        self.channel.default_customer_id = False
        node = self._order(wid=709)
        node['billing'] = {}
        with self._with_client():
            with self.assertRaises(UserError) as caught:
                self.channel._import_one_woo_order(node)
        self.assertIn('fallback customer', str(caught.exception))

    def test_an_unknown_product_can_be_refused_instead_of_invented(self):
        self.channel.create_missing_products = False
        node = self._order(wid=710)
        node['line_items'][0]['sku'] = 'NOT-IN-ODOO'
        node['line_items'][0]['product_id'] = 9999
        with self._with_client():
            with self.assertRaises(UserError) as caught:
                self.channel._import_one_woo_order(node)
        self.assertIn('NOT-IN-ODOO', str(caught.exception))

    def test_shipping_and_fees_arrive_as_their_own_lines(self):
        node = self._order(wid=711, total='118.00')
        node['shipping_lines'] = [{'method_title': 'Flat rate', 'total': '10.00',
                                   'taxes': []}]
        node['fee_lines'] = [{'name': 'Gift wrap', 'total': '8.00', 'taxes': []}]
        with self._with_client():
            order = self.channel._import_one_woo_order(node)
        self.assertEqual(len(order.order_line), 3)
        self.assertAlmostEqual(order.amount_total, 118.0, places=2)

    def test_the_order_date_comes_from_woocommerce_not_from_now(self):
        with self._with_client():
            order = self.channel._import_one_woo_order(self._order(wid=712))
        self.assertEqual((order.date_order.year, order.date_order.month,
                          order.date_order.day), (2026, 2, 1))

    def test_abandoned_checkouts_are_not_in_the_default_status_list(self):
        """pending and failed are abandoned carts, not sales."""
        statuses = (self.channel.woo_import_statuses or '')
        self.assertNotIn('pending', statuses)
        self.assertNotIn('failed', statuses)


class TestStockAndStatus(WooCase):

    def setUp(self):
        super().setUp()
        self.channel.export_stock = True
        self.product = self.env['product.product'].create(
            {'name': 'Stocked', 'type': 'consu', 'default_code': 'ST-1'})

    def test_publishing_is_refused_while_the_setting_is_off(self):
        self.channel.export_stock = False
        with self._with_client():
            with self.assertRaises(UserError):
                self.channel._export_woo_stock()

    def test_a_negative_quantity_is_never_sent(self):
        with patch.object(type(self.product), 'free_qty', -5):
            self.assertEqual(self.channel._quantity_for(self.product), 0)

    def test_an_untracked_product_is_skipped_rather_than_written_to(self):
        """Woo ignores stock_quantity when manage_stock is off.

        Sending it anyway would report success and change nothing.
        """
        self.product.woo_manage_stock = False
        self.env['miko.ecommerce.mapping'].link(self.channel, self.product, 800)
        with self._with_client():
            self.assertEqual(self.channel._export_woo_stock(), 0)

    def test_a_tracked_product_is_written_to(self):
        self.product.woo_manage_stock = True
        self.env['miko.ecommerce.mapping'].link(self.channel, self.product, 801)
        with self._with_client():
            self.assertEqual(self.channel._export_woo_stock(), 1)
        self.assertTrue(any(c[0] == 'PUT' for c in self.client.calls))

    def test_status_is_not_sent_while_the_setting_is_off(self):
        self.assertFalse(self.channel._woo_status_wanted())

    def test_status_push_sends_the_configured_status(self):
        self.channel.export_fulfilments = True
        order = self.env['sale.order'].create(
            {'partner_id': self.env['res.partner'].create({'name': 'B'}).id})
        self.env['miko.ecommerce.mapping'].link(self.channel, order, 900, '#900')
        with self._with_client():
            self.assertTrue(self.channel.push_woo_status(order, '1Z999'))
        put = [c for c in self.client.calls if c[0] == 'PUT'][0]
        self.assertEqual(put[3]['status'], 'completed')
        self.assertEqual(put[3]['meta_data'][0]['value'], '1Z999')

    def test_no_tracking_key_means_no_tracking_is_written(self):
        """A guessed meta key writes the number somewhere nobody looks."""
        self.channel.write({'export_fulfilments': True, 'woo_tracking_meta_key': ''})
        order = self.env['sale.order'].create(
            {'partner_id': self.env['res.partner'].create({'name': 'B'}).id})
        self.env['miko.ecommerce.mapping'].link(self.channel, order, 901, '#901')
        with self._with_client():
            self.channel.push_woo_status(order, '1Z999')
        put = [c for c in self.client.calls if c[0] == 'PUT'][0]
        self.assertNotIn('meta_data', put[3])

    def test_an_order_from_another_store_is_not_this_channels_business(self):
        self.channel.export_fulfilments = True
        other = self.env['sale.order'].create(
            {'partner_id': self.env['res.partner'].create({'name': 'C'}).id})
        with self._with_client():
            self.assertFalse(self.channel.push_woo_status(other))


class TestScheduledSync(WooCase):

    def test_installing_changes_nothing_until_it_is_switched_on(self):
        self.assertFalse(self.channel.auto_sync)
        ran = []
        with patch.object(type(self.channel), '_run_woo_sync',
                          side_effect=lambda: ran.append(1)):
            self.env['miko.ecommerce.channel']._cron_woo_sync()
        self.assertEqual(ran, [])

    def test_a_store_that_is_not_due_yet_is_skipped(self):
        self.channel.write({'auto_sync': True, 'sync_interval_minutes': 60,
                            'last_sync': fields.Datetime.now()})
        self.assertFalse(self.channel._woo_sync_due())

    def test_a_store_already_running_is_never_started_twice(self):
        self.channel.write({'auto_sync': True, 'sync_running': True})
        self.assertFalse(self.channel._woo_sync_due())

    def test_a_failing_sync_clears_the_running_flag(self):
        """Otherwise the store is stuck as running and never syncs again."""
        self.channel.write({'auto_sync': True, 'sync_orders': True})
        with patch.object(type(self.channel), '_import_woo_orders',
                          side_effect=Exception('boom')):
            self.channel._run_woo_sync()
        self.assertFalse(self.channel.sync_running)
        self.assertIn('Failed', self.channel.last_sync_message or '')


class TestFieldMapping(WooCase):

    def test_defaults_are_seeded_once_and_not_duplicated(self):
        self.channel._seed_field_maps()
        first = len(self.channel.field_map_ids)
        self.channel._seed_field_maps()
        self.assertEqual(len(self.channel.field_map_ids), first)
        self.assertTrue(first, 'WooCommerce must seed its own defaults')

    def test_a_field_that_does_not_exist_is_refused_when_typed(self):
        with self.assertRaises(ValidationError):
            self.env['miko.ecommerce.field.map'].create({
                'channel_id': self.channel.id, 'entity': 'product',
                'direction': 'in', 'shopify_field': 'name',
                'odoo_field': 'not_a_real_field'})

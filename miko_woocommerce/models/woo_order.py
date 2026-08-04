# -*- coding: utf-8 -*-
"""WooCommerce orders in, and order status back out.

Two things are genuinely different from Shopify and are handled as such rather
than forced into the same shape:

**WooCommerce has no fulfilment object.** There is nothing to create and nothing
to cancel. An order is "shipped" by moving its status to `completed`, and
tracking numbers are not a core field at all: they live in `meta_data`, under
whichever key the merchant's shipping plugin uses. So the tracking key is a
setting, defaulting to the widely used WooCommerce Shipment Tracking one, and if
it is left empty the status still moves and no tracking is written. Inventing a
key would silently write a number nowhere anyone looks.

**Tax arrives twice.** Order-level `tax_lines` carry the label and rate, and each
line item's `taxes` array references them by `rate_id`. The labels are what a
person recognises, so mapping is keyed on those, exactly as the Shopify connector
keys on tax line titles. Anything unmapped stops the order rather than producing
an invoice quietly short by the tax.
"""
import logging

from odoo import _, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Statuses that represent a real, payable order. `pending` and `failed` are
# abandoned checkouts and would fill Odoo with quotations nobody wants.
IMPORTABLE = ('processing', 'on-hold', 'completed')


def money(value):
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    woo_order_number = fields.Char(
        readonly=True, copy=False, index=True,
        help="The order number as the customer sees it in WooCommerce.")
    woo_total = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id',
        help="What WooCommerce said the customer was charged.")
    woo_total_matches = fields.Boolean(
        readonly=True, copy=False, default=True,
        help="False when the Odoo total does not agree with the WooCommerce "
             "total. Not safe to invoice until the difference is understood.")


class MikoEcommerceChannel(models.Model):
    _inherit = 'miko.ecommerce.channel'

    woo_import_statuses = fields.Char(
        string='Import orders with status', default=','.join(IMPORTABLE),
        help="Comma separated. Left at the default this skips pending and failed "
             "orders, which are abandoned checkouts rather than sales.")
    woo_completed_status = fields.Char(
        string='Status when shipped', default='completed',
        help="What the WooCommerce order is set to once the Odoo delivery is "
             "validated. Shops using a custom status can change it here.")
    woo_tracking_meta_key = fields.Char(
        string='Tracking meta key', default='_tracking_number',
        help="Which meta_data key the shipping plugin reads the tracking number "
             "from. WooCommerce has no core tracking field.\n\nLeave empty to "
             "move the status without writing tracking anywhere: a guessed key "
             "writes the number somewhere nobody looks.")
    export_fulfilments = fields.Boolean(
        string='Send order status to WooCommerce', default=False,
        help="When the Odoo delivery is validated, move the WooCommerce order to "
             "the shipped status. Off by default: this writes into a live shop.")

    # ------------------------------------------------------------- orders in
    def action_import_orders(self):
        for channel in self:
            channel._import_woo_orders()
        return True

    def _import_woo_orders(self):
        self.ensure_one()
        client = self._woo_client()
        Job = self.env['miko.ecommerce.job']
        Mapping = self.env['miko.ecommerce.mapping']

        statuses = [s.strip() for s in (self.woo_import_statuses or '').split(',') if s.strip()]
        params = {'status': ','.join(statuses)} if statuses else {}
        if self.import_from_date:
            params['after'] = fields.Datetime.to_string(
                self.import_from_date).replace(' ', 'T')

        count = 0
        for node in client.paginate('orders', params):
            job = Job.enqueue(self, 'import_order', node.get('id'), node,
                              external_ref=node.get('number'))
            # Checked before anything is written. This is what makes a re-run,
            # a crashed sync, or two overlapping schedules all harmless.
            if Mapping.already_imported(self, 'sale.order', node['id']):
                job.mark_skipped(_("Already imported."))
                continue
            try:
                order = self._job_import_order(node, job)
                job.mark_done(order)
                count += 1
            except Exception as err:        # noqa: BLE001 - kept, not lost
                _logger.exception("miko_woocommerce: order %s failed", node.get('number'))
                job.mark_failed(err, blocked=job._is_blocking(err),
                                guidance=job._guidance_for(err))
        self._touch_sync()
        return count

    def _job_import_order(self, payload, job=None):
        """Import one order, returning the existing one if it already arrived."""
        self.ensure_one()
        existing = self.env['miko.ecommerce.mapping'].find_odoo_record(
            self, 'sale.order', payload.get('id'))
        if existing:
            return existing
        return self._import_one_woo_order(payload)

    def _import_one_woo_order(self, node):
        self.ensure_one()
        Mapping = self.env['miko.ecommerce.mapping']
        partner = self._woo_order_partner(node)

        values = {
            'partner_id': partner.id,
            'company_id': self.company_id.id,
            'origin': node.get('number') or False,
            'client_order_ref': node.get('number') or False,
            'woo_order_number': node.get('number') or False,
            'woo_total': money(node.get('total')),
            'date_order': self._woo_datetime(node.get('date_created')),
            'order_line': self._woo_order_lines(node),
        }
        values.update(self._apply_maps_in('order', node, 'sale.order'))
        if self.team_id:
            values['team_id'] = self.team_id.id
        if self.warehouse_id:
            values['warehouse_id'] = self.warehouse_id.id
        if self.pricelist_id:
            values['pricelist_id'] = self.pricelist_id.id

        order = self.env['sale.order'].create(values)
        Mapping.link(self, order, node['id'], node.get('number'))
        self._verify_woo_total(order, node)

        if self.auto_confirm_orders and order.woo_total_matches:
            order.action_confirm()
            if self.auto_create_invoice:
                invoice = order._create_invoices()
                if invoice and self.journal_id:
                    invoice.journal_id = self.journal_id
        return order

    def _verify_woo_total(self, order, node):
        """Refuse to pretend the numbers agree when they do not."""
        expected, actual = money(node.get('total')), order.amount_total
        rounding = order.currency_id.rounding or 0.01
        if abs(expected - actual) <= max(rounding, 0.01):
            return True
        order.woo_total_matches = False
        order.message_post(body=_(
            "<p><b>This order was not imported at the price the customer paid.</b></p>"
            "<p>WooCommerce charged %(e)s. This order adds up to %(a)s, a "
            "difference of %(d)s.</p><p>Usually a discount, a shipping fee or a "
            "tax with no Odoo equivalent configured yet. It has been left as a "
            "quotation rather than confirmed, because invoicing it would bill the "
            "wrong amount.</p>") % {
                'e': '%.2f' % expected, 'a': '%.2f' % actual,
                'd': '%.2f' % (expected - actual)})
        _logger.warning("miko_woocommerce: total mismatch on %s: woo %.2f, odoo %.2f",
                        node.get('number'), expected, actual)
        return False

    @staticmethod
    def _woo_datetime(value):
        """Woo returns local-time ISO 8601 without a zone; treat it as naive."""
        if not value:
            return fields.Datetime.now()
        text = str(value).replace('T', ' ').split('.')[0].split('+')[0].replace('Z', '')
        return fields.Datetime.to_datetime(text.strip()) or fields.Datetime.now()

    def _woo_order_partner(self, node):
        """Who to bill. Never guessed, never left blank."""
        customer_id = node.get('customer_id')
        if customer_id:
            partner = self.env['miko.ecommerce.mapping'].find_odoo_record(
                self, 'res.partner', customer_id)
            if partner:
                return partner
            fetched, _h = self._woo_client().call('GET', 'customers/%s' % customer_id)
            return self._upsert_woo_customer(fetched)
        # Guest checkout. Woo sends customer_id 0 and only the billing block.
        billing = node.get('billing') or {}
        email = (billing.get('email') or '').strip()
        if email:
            return self._upsert_woo_customer({
                'id': 'guest-%s' % node['id'], 'email': email,
                'first_name': billing.get('first_name'),
                'last_name': billing.get('last_name'), 'billing': billing})
        if self.default_customer_id:
            return self.default_customer_id
        raise UserError(_(
            "WooCommerce order %s is a guest checkout with no email address, and "
            "this store has no fallback customer set. Set one on the store record "
            "so guest orders have somewhere to go.") % (node.get('number') or ''))

    # ------------------------------------------------------------------ lines
    def _woo_order_lines(self, node):
        commands = []
        rates = {str(t.get('rate_id')): t for t in (node.get('tax_lines') or [])}
        for item in node.get('line_items') or []:
            commands.append((0, 0, self._woo_product_line(item, rates)))
        for ship in node.get('shipping_lines') or []:
            if money(ship.get('total')):
                commands.append((0, 0, self._woo_shipping_line(ship, rates)))
        for fee in node.get('fee_lines') or []:
            if money(fee.get('total')):
                commands.append((0, 0, self._woo_fee_line(fee, rates)))
        return commands

    def _sol_tax_field(self):
        """sale.order.line.tax_id became tax_ids in Odoo 19.

        Resolved from the registry rather than hardcoded: writing the wrong one
        raises at create() time, so the whole import dies on exactly one series
        and works everywhere else.
        """
        return 'tax_ids' if 'tax_ids' in self.env['sale.order.line']._fields else 'tax_id'

    def _woo_product_line(self, item, rates):
        product = self._resolve_woo_product(item)
        qty = float(item.get('quantity') or 0.0) or 1.0
        gross = money(item.get('subtotal'))          # before line discounts
        net = money(item.get('total'))               # after them
        unit = gross / qty if qty else gross
        discount = 0.0
        if gross and net < gross:
            # Kept as a discount rather than folded into the price, so the order
            # still shows what the product normally sells for and every margin
            # report downstream stays truthful.
            discount = round((gross - net) / gross * 100.0, 4)
        return {
            'product_id': product.id,
            'name': item.get('name') or product.display_name,
            'product_uom_qty': qty,
            'price_unit': unit,
            'discount': discount,
            self._sol_tax_field(): [(6, 0, self._resolve_woo_taxes(item, rates).ids)],
        }

    def _woo_shipping_line(self, ship, rates):
        return {
            'product_id': self._woo_delivery_product().id,
            'name': ship.get('method_title') or _('Shipping'),
            'product_uom_qty': 1.0,
            'price_unit': money(ship.get('total')),
            self._sol_tax_field(): [(6, 0, self._resolve_woo_taxes(ship, rates).ids)],
        }

    def _woo_fee_line(self, fee, rates):
        return {
            'product_id': self._woo_fee_product().id,
            'name': fee.get('name') or _('Fee'),
            'product_uom_qty': 1.0,
            'price_unit': money(fee.get('total')),
            self._sol_tax_field(): [(6, 0, self._resolve_woo_taxes(fee, rates).ids)],
        }

    def _resolve_woo_taxes(self, line, rates):
        """Odoo taxes for a line, honouring the store's unmapped-tax policy."""
        TaxMap = self.env['miko.ecommerce.tax']
        taxes = self.env['account.tax'].browse()
        for applied in line.get('taxes') or []:
            if not money(applied.get('total')) and not money(applied.get('subtotal')):
                continue
            header = rates.get(str(applied.get('id'))) or {}
            title = (header.get('label') or _('Tax')).strip()
            try:
                rate = float(header.get('rate_percent') or 0.0) / 100.0
            except (TypeError, ValueError):
                rate = 0.0
            found = TaxMap.resolve(self, {'title': title, 'rate': rate})
            if found:
                taxes |= found
            elif self.unmapped_tax_policy == 'block':
                raise UserError(_(
                    "WooCommerce sent a '%(title)s' tax at %(rate)s%% and there "
                    "is no Odoo tax mapped to it yet.\n\nMap it on the store's "
                    "Taxes tab, then retry this order. Importing without it would "
                    "produce an invoice short by the tax amount.") % {
                        'title': title, 'rate': round(rate * 100, 4)})
        return taxes

    def _resolve_woo_product(self, item):
        """The Odoo product for a line, in order of how sure we can be."""
        Mapping = self.env['miko.ecommerce.mapping']
        Product = self.env['product.product']
        for key in ('variation_id', 'product_id'):
            ext = item.get(key)
            if ext:
                product = Mapping.find_odoo_record(self, 'product.product', ext)
                if product:
                    return product
        sku = (item.get('sku') or '').strip()
        if sku:
            product = Product.search([('default_code', '=', sku)], limit=1)
            if product:
                ext = item.get('variation_id') or item.get('product_id')
                if ext:
                    Mapping.link(self, product, ext, sku)
                return product
        if not self.create_missing_products:
            raise UserError(_(
                "Order line '%(title)s'%(sku)s does not match any Odoo product, "
                "and this store is set not to create products.\n\nImport the "
                "catalogue first, or set the SKU in Odoo to match WooCommerce.") % {
                    'title': item.get('name') or '?',
                    'sku': ' (SKU %s)' % sku if sku else ''})
        qty = float(item.get('quantity') or 1.0) or 1.0
        product = Product.create({
            'name': item.get('name') or _('WooCommerce product'),
            'default_code': sku or False, 'type': 'consu',
            'list_price': money(item.get('subtotal')) / qty, 'sale_ok': True})
        ext = item.get('variation_id') or item.get('product_id')
        if ext:
            Mapping.link(self, product, ext, sku)
        _logger.info("miko_woocommerce: created product '%s' from an order line",
                     product.display_name)
        return product

    def _service_product(self, reference, name, ptype='service'):
        product = self.env['product.product'].with_context(active_test=False).search(
            [('default_code', '=', reference)], limit=1)
        if product:
            return product
        return self.env['product.product'].create({
            'name': name, 'default_code': reference, 'type': ptype,
            'invoice_policy': 'order', 'list_price': 0.0,
            'sale_ok': True, 'purchase_ok': False})

    def _woo_delivery_product(self):
        self.ensure_one()
        return self._service_product('MIKO-WOO-SHIP-%s' % self.id,
                                     _('Shipping (%s)') % self.name)

    def _woo_fee_product(self):
        self.ensure_one()
        return self._service_product('MIKO-WOO-FEE-%s' % self.id,
                                     _('Fee (%s)') % self.name)

    # ---------------------------------------------------------- status out
    def _woo_status_wanted(self):
        self.ensure_one()
        return self.platform == 'woocommerce' and self.export_fulfilments

    def push_woo_status(self, order, tracking=None):
        """Move the WooCommerce order to the shipped status. Returns True/False.

        WooCommerce has no fulfilment object, so this is the whole of "mark it
        shipped": a status change, plus the tracking number in meta_data if a key
        has been configured.
        """
        self.ensure_one()
        row = self.env['miko.ecommerce.mapping'].search([
            ('channel_id', '=', self.id), ('model_name', '=', 'sale.order'),
            ('odoo_id', '=', order.id)], limit=1)
        if not row:
            return False
        body = {'status': (self.woo_completed_status or 'completed').strip()}
        key = (self.woo_tracking_meta_key or '').strip()
        if tracking and key:
            body['meta_data'] = [{'key': key, 'value': tracking}]
        job = self.env['miko.ecommerce.job'].enqueue(
            self, 'export_status', row.external_id, body,
            external_ref=order.name, direction='out')
        return self._job_export_status(body, job)

    def _job_export_status(self, payload, job=None):
        """Re-runnable, so Retry means something."""
        self.ensure_one()
        external = (job.external_id if job else None)
        if not external:
            raise UserError(_("This job has lost the WooCommerce order id."))
        self._woo_client().call('PUT', 'orders/%s' % external, payload=payload)
        if job:
            job.mark_done()
        return True


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    woo_status_pushed = fields.Boolean(
        readonly=True, copy=False,
        help="Set once this delivery moved the WooCommerce order to its shipped "
             "status, so the same delivery never does it twice.")

    def _action_done(self):
        result = super()._action_done()
        for picking in self:
            try:
                picking._miko_push_woo_status()
            except Exception:        # noqa: BLE001
                # A delivery must always complete in Odoo. A shop being
                # unreachable cannot be allowed to block the warehouse.
                _logger.exception(
                    "miko_woocommerce: could not update order status for %s", picking.name)
        return result

    def _miko_push_woo_status(self):
        self.ensure_one()
        if self.picking_type_id.code != 'outgoing' or self.state != 'done':
            return False
        if self.woo_status_pushed:
            return False
        order = (self.sale_id if 'sale_id' in self._fields else
                 self.env['sale.order'].browse())
        if not order:
            return False
        channel = self.env['miko.ecommerce.mapping']._channel_for(order)
        if not channel or not channel._woo_status_wanted():
            return False
        tracking = (self.carrier_tracking_ref
                    if 'carrier_tracking_ref' in self._fields else None)
        if channel.push_woo_status(order, (tracking or '').strip() or None):
            self.woo_status_pushed = True
            return True
        return False

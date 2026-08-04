# -*- coding: utf-8 -*-
"""Products, variations, customers and stock, both directions.

Where this differs from the Shopify connector, it is because WooCommerce differs,
not because it was written later:

* **Variations are a separate endpoint.** A `variable` product's variations live
  at `/products/<id>/variations`, so importing a catalogue is two calls per
  variable product rather than one nested query.
* **A variation's options come as `attributes: [{name, option}]`** with no ids we
  can rely on, so matching is on the option NAMES, exactly as the Shopify path
  matches on selected options. Exact set equality, never a subset: a near match
  puts a sale on the wrong SKU.
* **Stock lives on the product or variation record itself** (`stock_quantity`),
  so publishing stock is a PUT on that record. There is no separate inventory
  API and no location concept, which makes this simpler than Shopify - but it
  also means `manage_stock` has to be true or Woo ignores the number entirely.
* **Customers have first/last name and billing/shipping blocks** as plain nested
  objects, with none of Shopify's nested email indirection.
"""
import logging

from odoo import _, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

PLACEHOLDER_ATTR = ('any', '')


class ProductProduct(models.Model):
    _inherit = 'product.product'

    woo_manage_stock = fields.Boolean(
        readonly=True, copy=False,
        help="Whether WooCommerce is tracking stock for this item. When false, "
             "Woo ignores any quantity sent for it.")


class MikoEcommerceChannel(models.Model):
    _inherit = 'miko.ecommerce.channel'

    export_stock = fields.Boolean(
        string='Publish stock to WooCommerce', default=False,
        help="Off by default. Writing stock into a live shop is not something a "
             "module should start doing because it was installed.")
    stock_source = fields.Selection(
        [('free', 'Available to promise'), ('on_hand', 'On hand')],
        default='free', required=True, string='Quantity to publish',
        help="Available to promise excludes stock already reserved for other "
             "orders. Publishing on hand is how the same unit gets sold twice.")

    # ------------------------------------------------------- products, inbound
    def action_import_products(self):
        for channel in self:
            channel._import_woo_products()
        return True

    def _import_woo_products(self):
        self.ensure_one()
        self._require_direction('product_direction', 'in', _("Products"))
        client = self._woo_client()
        Job = self.env['miko.ecommerce.job']
        params = {}
        if self.import_from_date:
            params['modified_after'] = fields.Datetime.to_string(
                self.import_from_date).replace(' ', 'T')

        seen = 0
        for node in client.paginate('products', params):
            job = Job.enqueue(self, 'import_product', node.get('id'), node,
                              external_ref=node.get('name'))
            try:
                template = self._job_import_product(node, job)
                job.mark_done(template)
                seen += 1
            except Exception as err:        # noqa: BLE001 - kept, not lost
                _logger.exception("miko_woocommerce: product %s failed", node.get('id'))
                job.mark_failed(err, blocked=job._is_blocking(err),
                                guidance=job._guidance_for(err))
        self._touch_sync()
        return seen

    def _job_import_product(self, payload, job=None):
        """Import one product and its variations. Shared with Retry."""
        self.ensure_one()
        Mapping = self.env['miko.ecommerce.mapping']
        Template = self.env['product.template']

        template = Mapping.find_odoo_record(self, 'product.template', payload['id'])
        values = {
            'name': payload.get('name') or _('Unnamed WooCommerce product'),
            'type': 'service' if payload.get('virtual') else 'consu',
            'sale_ok': True,
        }
        values.update(self._apply_maps_in('product', payload, 'product.template'))
        if not template:
            template = Template.create(values)
            # Linked in the same breath as the create: anything between the two
            # is a window where a crash leaves an orphan the next run cannot
            # recognise, and therefore creates again.
            Mapping.link(self, template, payload['id'], payload.get('slug'))
        elif template.name != values['name']:
            template.name = values['name']

        if payload.get('type') == 'variable':
            self._import_woo_variations(payload, template)
        else:
            self._link_simple_product(payload, template)
        return template

    def _link_simple_product(self, payload, template):
        """A simple product is one Odoo variant, so link that."""
        product = template.product_variant_id
        if not product:
            return
        self._write_variant_fields(product, payload)
        self.env['miko.ecommerce.mapping'].link(
            self, product, payload['id'], payload.get('sku') or payload.get('name'))

    def _import_woo_variations(self, payload, template):
        """Fetch and link the variations of a variable product."""
        client = self._woo_client()
        Mapping = self.env['miko.ecommerce.mapping']
        variations = list(client.paginate('products/%s/variations' % payload['id']))
        self._apply_woo_attributes(payload, template)

        for var in variations:
            product = Mapping.find_odoo_record(self, 'product.product', var['id'])
            if not product:
                product = self._match_woo_variation(var, template)
            if not product:
                # Never attach a sale to a nearly-matching variant. Reported and
                # left alone instead.
                _logger.warning(
                    "miko_woocommerce: variation %s of '%s' has no matching Odoo "
                    "variant and was left unlinked",
                    var.get('sku') or var.get('id'), template.name)
                continue
            self._write_variant_fields(product, var)
            Mapping.link(self, product, var['id'], var.get('sku'))

    def _apply_woo_attributes(self, payload, template):
        """Mirror Woo's attributes as Odoo attribute lines. Only ever adds.

        Removing an attribute line deletes the variants underneath it, and those
        are referenced by every historical order line, so a sync must never do it
        as a side effect.
        """
        Attribute = self.env['product.attribute']
        Value = self.env['product.attribute.value']
        for attr in payload.get('attributes') or []:
            if not attr.get('variation'):
                continue                     # not a variation axis, just a spec
            name = (attr.get('name') or '').strip()
            options = [o.strip() for o in (attr.get('options') or []) if (o or '').strip()]
            if not name or not options:
                continue
            attribute = Attribute.search([('name', '=', name)], limit=1) or \
                Attribute.create({'name': name, 'create_variant': 'always'})
            wanted = []
            for label in options:
                value = Value.search([('attribute_id', '=', attribute.id),
                                      ('name', '=', label)], limit=1) or \
                    Value.create({'attribute_id': attribute.id, 'name': label})
                wanted.append(value.id)
            line = template.attribute_line_ids.filtered(
                lambda l, a=attribute: l.attribute_id == a)
            if line:
                missing = [v for v in wanted if v not in line.value_ids.ids]
                if missing:
                    line.value_ids = [(4, v) for v in missing]
            else:
                template.attribute_line_ids = [(0, 0, {
                    'attribute_id': attribute.id, 'value_ids': [(6, 0, wanted)]})]

    @staticmethod
    def _option_key(pairs):
        """Comparable, order-independent key for a set of option values."""
        return frozenset(
            ((n or '').strip().lower(), (v or '').strip().lower())
            for n, v in pairs if (v or '').strip().lower() not in PLACEHOLDER_ATTR)

    def _match_woo_variation(self, var, template):
        """The Odoo variant whose option values are exactly this variation's."""
        products = template.product_variant_ids
        if not products:
            return self.env['product.product'].browse()
        wanted = self._option_key(
            (a.get('name'), a.get('option')) for a in (var.get('attributes') or []))
        if not wanted:
            return products if len(products) == 1 else self.env['product.product'].browse()
        for product in products:
            actual = self._option_key(
                (v.attribute_id.name, v.name)
                for v in product.product_template_attribute_value_ids)
            if actual == wanted:
                return product
        return self.env['product.product'].browse()

    def _write_variant_fields(self, product, node):
        """SKU, price and stock flags, without ever clashing a barcode."""
        updates = {}
        sku = (node.get('sku') or '').strip()
        if sku and product.default_code != sku:
            updates['default_code'] = sku
        price = node.get('regular_price') or node.get('price')
        if price not in (None, ''):
            try:
                updates['lst_price'] = float(price)
            except (TypeError, ValueError):
                pass
        manage = bool(node.get('manage_stock'))
        if product.woo_manage_stock != manage:
            updates['woo_manage_stock'] = manage
        if updates:
            product.write(updates)

    # ------------------------------------------------------ products, outbound
    def action_export_products(self):
        for channel in self:
            channel._export_woo_products()
        return True

    def _export_woo_products(self):
        self.ensure_one()
        self._require_direction('product_direction', 'out', _("Products"))
        Job = self.env['miko.ecommerce.job']
        count = 0
        for template in self.env['product.template'].search([
                ('sale_ok', '=', True),
                '|', ('company_id', '=', self.company_id.id), ('company_id', '=', False)]):
            job = Job.enqueue(self, 'export_product', None,
                              {'template_id': template.id},
                              external_ref=template.name, direction='out')
            try:
                self._job_export_product(job.get_payload(), job)
            except Exception as err:        # noqa: BLE001 - kept, not lost
                _logger.exception("miko_woocommerce: export of %s failed", template.name)
                job.mark_failed(err, blocked=job._is_blocking(err),
                                guidance=job._guidance_for(err))
            else:
                job.mark_done(template)
                count += 1
        self._touch_sync()
        return count

    def _job_export_product(self, payload, job=None):
        self.ensure_one()
        template = self.env['product.template'].browse(
            (payload or {}).get('template_id') or 0).exists()
        if not template:
            raise UserError(_(
                "The Odoo product this job refers to no longer exists. The job "
                "can be deleted."))
        Mapping = self.env['miko.ecommerce.mapping']
        row = Mapping.search([('channel_id', '=', self.id),
                              ('model_name', '=', 'product.template'),
                              ('odoo_id', '=', template.id)], limit=1)
        body = {'name': template.name,
                'type': 'simple',
                'regular_price': str(template.list_price or 0.0)}
        body.update(self._apply_maps_out('product', template))
        client = self._woo_client()
        if row:
            created, _h = client.call('PUT', 'products/%s' % row.external_id, payload=body)
        else:
            created, _h = client.call('POST', 'products', payload=body)
        if created.get('id'):
            Mapping.link(self, template, created['id'], created.get('slug'))
        return template

    # ----------------------------------------------------------------- stock
    def action_export_stock(self):
        for channel in self:
            channel._export_woo_stock()
        return True

    def _quantity_for(self, product):
        self.ensure_one()
        if self.warehouse_id:
            product = product.with_context(warehouse_id=self.warehouse_id.id)
        value = product.free_qty if self.stock_source == 'free' else product.qty_available
        # Woo will take a negative, but a negative available quantity on a
        # storefront is never what anybody means.
        return max(0, int(value or 0))

    def _export_woo_stock(self):
        self.ensure_one()
        if not self.export_stock:
            raise UserError(_(
                "Publishing stock is switched off for %s. Turn on 'Publish stock "
                "to WooCommerce' on the store first.") % self.name)
        client = self._woo_client()
        Job = self.env['miko.ecommerce.job']
        rows = self.env['miko.ecommerce.mapping'].search([
            ('channel_id', '=', self.id), ('model_name', '=', 'product.product')])
        sent, untracked = 0, 0

        for row in rows:
            product = self.env['product.product'].browse(row.odoo_id).exists()
            if not product or product.type != 'consu':
                continue
            if not product.woo_manage_stock:
                # Woo silently ignores stock_quantity when manage_stock is off,
                # so sending it would report success and change nothing.
                untracked += 1
                continue
            job = Job.enqueue(self, 'export_stock', row.external_id,
                              {'mapping_id': row.id}, external_ref=product.default_code,
                              direction='out')
            try:
                self._job_export_stock(job.get_payload(), job)
            except Exception as err:        # noqa: BLE001 - kept, not lost
                job.mark_failed(err, blocked=job._is_blocking(err),
                                guidance=job._guidance_for(err))
            else:
                job.mark_done(product)
                sent += 1
        if untracked:
            _logger.warning(
                "miko_woocommerce: %s product(s) have stock management switched "
                "off in WooCommerce, so no quantity was sent for them", untracked)
        self._touch_sync()
        return sent

    def _job_export_stock(self, payload, job=None):
        self.ensure_one()
        row = self.env['miko.ecommerce.mapping'].browse(
            (payload or {}).get('mapping_id') or 0).exists()
        if not row:
            raise UserError(_("That link no longer exists; the job can be deleted."))
        product = self.env['product.product'].browse(row.odoo_id).exists()
        if not product:
            raise UserError(_("The Odoo product no longer exists."))
        qty = self._quantity_for(product)
        # A variation is a different endpoint from a simple product, and the
        # parent id is needed to address it.
        parent = self.env['miko.ecommerce.mapping'].search([
            ('channel_id', '=', self.id), ('model_name', '=', 'product.template'),
            ('odoo_id', '=', product.product_tmpl_id.id)], limit=1)
        if parent and parent.external_id != row.external_id:
            path = 'products/%s/variations/%s' % (parent.external_id, row.external_id)
        else:
            path = 'products/%s' % row.external_id
        self._woo_client().call('PUT', path, payload={'stock_quantity': qty})
        return product

    # ------------------------------------------------------------- customers
    def action_import_customers(self):
        for channel in self:
            channel._import_woo_customers()
        return True

    def _import_woo_customers(self):
        self.ensure_one()
        self._require_direction('customer_direction', 'in', _("Customers"))
        client = self._woo_client()
        Job = self.env['miko.ecommerce.job']
        count = 0
        for node in client.paginate('customers', {'role': 'all'}):
            job = Job.enqueue(self, 'import_customer', node.get('id'), node,
                              external_ref=node.get('email'))
            try:
                partner = self._job_import_customer(node, job)
                job.mark_done(partner)
                count += 1
            except Exception as err:        # noqa: BLE001 - kept, not lost
                _logger.exception("miko_woocommerce: customer %s failed", node.get('id'))
                job.mark_failed(err, blocked=job._is_blocking(err),
                                guidance=job._guidance_for(err))
        self._touch_sync()
        return count

    def _job_import_customer(self, payload, job=None):
        self.ensure_one()
        return self._upsert_woo_customer(payload)

    def _upsert_woo_customer(self, node):
        Mapping = self.env['miko.ecommerce.mapping']
        Partner = self.env['res.partner']
        partner = Mapping.find_odoo_record(self, 'res.partner', node['id'])
        email = (node.get('email') or '').strip()

        if not partner and email:
            # Exact email only. Anything looser merges strangers, and contacts do
            # not come apart again once orders are attached.
            partner = Partner.search([
                ('email', '=ilike', email), ('parent_id', '=', False),
                '|', ('company_id', '=', self.company_id.id),
                     ('company_id', '=', False)], limit=1)

        values = self._woo_partner_values(node)
        if not partner:
            partner = Partner.create(values)
        else:
            # Fill what is empty, never overwrite what somebody typed in Odoo.
            partner.write({k: v for k, v in values.items() if v and not partner[k]})
        Mapping.link(self, partner, node['id'], email or partner.name)
        return partner

    def _woo_partner_values(self, node):
        billing = node.get('billing') or {}
        name = ' '.join(p for p in [(node.get('first_name') or '').strip(),
                                    (node.get('last_name') or '').strip()] if p)
        if not name:
            name = (billing.get('company') or '').strip()
        if not name:
            # Never create a nameless contact; it is unfindable afterwards.
            name = (node.get('email') or '').strip() or _('WooCommerce customer')
        values = {
            'name': name,
            'email': (node.get('email') or '').strip() or False,
            'phone': (billing.get('phone') or '').strip() or False,
            'customer_rank': 1,
            'company_id': self.company_id.id,
        }
        values.update(self._woo_address_values(billing))
        values.update(self._apply_maps_in('customer', node, 'res.partner'))
        return values

    def _woo_address_values(self, address):
        """Woo address fields as Odoo ones, resolving country by ISO code."""
        if not address:
            return {}
        values = {
            'street': (address.get('address_1') or '').strip() or False,
            'street2': (address.get('address_2') or '').strip() or False,
            'city': (address.get('city') or '').strip() or False,
            'zip': (address.get('postcode') or '').strip() or False,
        }
        code = (address.get('country') or '').strip().upper()
        if code:
            country = self.env['res.country'].search([('code', '=', code)], limit=1)
            if country:
                values['country_id'] = country.id
                state_code = (address.get('state') or '').strip().upper()
                if state_code:
                    state = self.env['res.country.state'].search([
                        ('country_id', '=', country.id),
                        ('code', '=', state_code)], limit=1)
                    if state:
                        values['state_id'] = state.id
            else:
                _logger.warning(
                    "miko_woocommerce: country code %s is not in Odoo; the "
                    "address was imported without it", code)
        return values

    def action_export_customers(self):
        for channel in self:
            channel._export_woo_customers()
        return True

    def _export_woo_customers(self):
        self.ensure_one()
        self._require_direction('customer_direction', 'out', _("Customers"))
        Job = self.env['miko.ecommerce.job']
        count = 0
        for partner in self.env['res.partner'].search([
                ('customer_rank', '>', 0), ('email', '!=', False),
                ('parent_id', '=', False),
                '|', ('company_id', '=', self.company_id.id),
                     ('company_id', '=', False)]):
            job = Job.enqueue(self, 'export_customer', None,
                              {'partner_id': partner.id},
                              external_ref=partner.name, direction='out')
            try:
                self._job_export_customer(job.get_payload(), job)
            except Exception as err:        # noqa: BLE001 - kept, not lost
                job.mark_failed(err, blocked=job._is_blocking(err),
                                guidance=job._guidance_for(err))
            else:
                job.mark_done(partner)
                count += 1
        self._touch_sync()
        return count

    def _job_export_customer(self, payload, job=None):
        self.ensure_one()
        partner = self.env['res.partner'].browse(
            (payload or {}).get('partner_id') or 0).exists()
        if not partner:
            raise UserError(_(
                "The Odoo contact this job refers to no longer exists. The job "
                "can be deleted."))
        if not partner.email:
            raise UserError(_(
                "%s has no email address. WooCommerce identifies customers by "
                "email, so without one this contact would be created again on "
                "every single run.") % partner.display_name)
        Mapping = self.env['miko.ecommerce.mapping']
        row = Mapping.search([('channel_id', '=', self.id),
                              ('model_name', '=', 'res.partner'),
                              ('odoo_id', '=', partner.id)], limit=1)
        first, _sep, last = (partner.name or '').partition(' ')
        body = {'email': partner.email, 'first_name': first or partner.name,
                'last_name': last or ''}
        body.update(self._apply_maps_out('customer', partner))
        client = self._woo_client()
        if row:
            created, _h = client.call('PUT', 'customers/%s' % row.external_id, payload=body)
        else:
            created, _h = client.call('POST', 'customers', payload=body)
        if created.get('id'):
            Mapping.link(self, partner, created['id'], partner.email)
        return partner

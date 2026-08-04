# -*- coding: utf-8 -*-
"""WooCommerce credentials, direction control and the scheduled sync.

The defaults are conservative in the same way the Shopify connector's are, and
for the same reason: a connector that starts confirming, invoicing and writing to
a live shop the moment it is installed makes a mess before anyone agreed to it.
Everything that writes is off until somebody switches it on.
"""
import logging
import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import config

from .woo_client import WooClient, WooError

_logger = logging.getLogger(__name__)

DIRECTIONS = [
    ('in', 'Store to Odoo'),
    ('out', 'Odoo to store'),
    ('both', 'Both ways'),
]


class MikoEcommerceChannel(models.Model):
    _inherit = 'miko.ecommerce.channel'

    platform = fields.Selection(
        selection_add=[('woocommerce', 'WooCommerce')],
        ondelete={'woocommerce': 'set default'})

    woo_url = fields.Char(
        string='Store address',
        help="The site root, such as https://example.com. Not the wp-admin URL "
             "and not the /wp-json path.")
    woo_key = fields.Char(
        string='Consumer key', groups='base.group_system',
        help="From WooCommerce, Settings, Advanced, REST API. Needs Read/Write.")
    woo_secret = fields.Char(
        string='Consumer secret', groups='base.group_system',
        help="Shown once, when the key is created.\n\nStored in this database "
             "like any other setting, so treat database access as equivalent to "
             "store access, and revoke the key if that stops being true.")

    woo_version = fields.Char(string='WooCommerce version', readonly=True)
    woo_wp_version = fields.Char(string='WordPress version', readonly=True)

    product_direction = fields.Selection(
        DIRECTIONS, default='in', required=True, string='Products')
    customer_direction = fields.Selection(
        DIRECTIONS, default='in', required=True, string='Customers')

    auto_sync = fields.Boolean(
        string='Sync on a schedule', default=False,
        help="Off until you turn it on. Installing a connector should never "
             "start moving data on its own.")
    sync_interval_minutes = fields.Integer(string='Every (minutes)', default=60)
    sync_orders = fields.Boolean(string='Sync orders', default=True)
    sync_products = fields.Boolean(string='Sync products', default=False)
    sync_customers = fields.Boolean(string='Sync customers', default=False)
    sync_running = fields.Boolean(
        readonly=True, copy=False,
        help="Set while a scheduled run is in progress, so a long sync is never "
             "started a second time on top of itself.")
    last_sync_message = fields.Text(readonly=True, copy=False)

    # ------------------------------------------------------------------
    @api.constrains('woo_url', 'platform')
    def _check_woo_url(self):
        for channel in self:
            if channel.platform != 'woocommerce' or not channel.woo_url:
                continue
            url = channel.woo_url.strip()
            if not url.startswith('https://'):
                raise ValidationError(_(
                    "The store address must start with https://.\n\n"
                    "WooCommerce sends the consumer key and secret with every "
                    "request. Over plain HTTP anyone on the network can read "
                    "them."))
            if '/wp-json' in url or '/wp-admin' in url:
                raise ValidationError(_(
                    "Use the site root, such as https://example.com. The "
                    "connector adds /wp-json/wc/v3 itself."))

    @api.onchange('woo_url')
    def _onchange_woo_url(self):
        """Repair what people actually paste, rather than refusing it."""
        if not self.woo_url:
            return
        url = self.woo_url.strip().rstrip('/')
        url = re.sub(r'/(wp-admin|wp-json)(/.*)?$', '', url)
        if url.startswith('http://'):
            url = 'https://' + url[len('http://'):]
        elif not url.startswith('https://') and '.' in url:
            url = 'https://' + url
        self.woo_url = url

    # ------------------------------------------------------------------
    def _woo_client(self):
        self.ensure_one()
        if self.platform != 'woocommerce':
            raise UserError(_("%s is not a WooCommerce store.") % self.name)
        me = self.sudo()
        return WooClient(self.woo_url, me.woo_key, me.woo_secret)

    def action_test_connection(self):
        """Prove the credentials work, and say precisely what failed if not."""
        for channel in self:
            if channel.platform != 'woocommerce':
                continue
            try:
                info = channel._woo_client().system_status()
            except WooError as err:
                channel.write({'connection_state': 'error',
                               'connection_message': str(err)})
                continue
            except Exception as err:            # noqa: BLE001 - shown to the user
                channel.write({'connection_state': 'error',
                               'connection_message': _("Unexpected problem: %s") % err})
                continue
            channel.write({
                'connection_state': 'ok',
                'woo_version': info.get('wc_version'),
                'woo_wp_version': info.get('wp_version'),
                'connection_message': _(
                    "Connected to %(name)s. WooCommerce %(wc)s on WordPress "
                    "%(wp)s, selling in %(cur)s.") % {
                        'name': info.get('name'), 'wc': info.get('wc_version'),
                        'wp': info.get('wp_version'), 'cur': info.get('currency')},
            })
        return True

    def _require_direction(self, setting, wanted, what):
        """Refuse politely when this is not the direction the store chose."""
        self.ensure_one()
        value = self[setting]
        if value in (wanted, 'both'):
            return True
        raise UserError(_(
            "%(what)s on %(store)s is set to '%(current)s', so it cannot be sent "
            "the other way.\n\nChange it on the store's Directions tab if that is "
            "what you want.") % {
                'what': what, 'store': self.name,
                'current': dict(DIRECTIONS).get(value, value)})

    # ------------------------------------------------------------------
    @api.model
    def _cron_woo_sync(self):
        """Entry point for the scheduler. Never raises."""
        for channel in self.search([('platform', '=', 'woocommerce'),
                                    ('active', '=', True),
                                    ('auto_sync', '=', True)]):
            if channel._woo_sync_due():
                channel._run_woo_sync()
        return True

    def _woo_sync_due(self):
        self.ensure_one()
        if self.sync_running:
            _logger.info("miko_woocommerce: %s is still syncing, skipping", self.name)
            return False
        if not self.last_sync:
            return True
        minutes = max(self.sync_interval_minutes or 0, 5)
        return (fields.Datetime.now() - self.last_sync).total_seconds() / 60.0 >= minutes

    def _checkpoint(self, rollback=False):
        """Commit progress as the run goes, except under test.

        config['test_enable'], not Registry.in_test_mode(): the latter reports
        whether the registry holds a test cursor, which is False for an ordinary
        at-install TransactionCase, so trusting it means really committing during
        the suite and aborting the transaction.
        """
        if config['test_enable']:
            return False
        if rollback:
            self.env.cr.rollback()
        else:
            self.env.cr.commit()
        return True

    def _run_woo_sync(self):
        self.ensure_one()
        self.sync_running = True
        self._checkpoint()
        done = []
        try:
            if self.sync_products:
                done.append(_("%s products") % self._import_woo_products())
            if self.sync_customers:
                done.append(_("%s customers") % self._import_woo_customers())
            if self.sync_orders:
                done.append(_("%s orders") % self._import_woo_orders())
            message = _("Synced %s.") % (", ".join(done) or _("nothing enabled"))
        except Exception as err:          # noqa: BLE001 - recorded, never raised on
            _logger.exception("miko_woocommerce: scheduled sync failed for %s", self.name)
            self._checkpoint(rollback=True)
            message = _("Failed: %s") % err
        finally:
            # Always clears, including after a rollback, or the channel is stuck
            # as "running" for ever and never syncs again.
            self.sync_running = False
            self.last_sync_message = message
            self._checkpoint()
        return message

    def action_sync_now(self):
        for channel in self:
            channel._run_woo_sync()
        return True


    def _default_field_maps(self):
        """WooCommerce's own default mappings, seeded per store.

        The engine holds the mechanism and knows nothing about any platform's
        field names; this is where WooCommerce's live.
        """
        if self.platform != 'woocommerce':
            return super()._default_field_maps()
        return [
            ('product', 'in', 'description', 'description_sale', False),
            ('product', 'in', 'short_description', 'description_sale', False),
            ('product', 'out', 'description', 'description_sale', True),
            ('customer', 'in', 'email', 'email', True),
            ('customer', 'out', 'email', 'email', True),
            ('order', 'in', 'customer_note', 'note', False),
        ]

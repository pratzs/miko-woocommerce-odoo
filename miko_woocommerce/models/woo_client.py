# -*- coding: utf-8 -*-
"""The WooCommerce REST client.

WooCommerce is not Shopify, and pretending otherwise is how a connector ends up
subtly wrong. The differences that actually shape this file:

* **It is the merchant's own WordPress server**, not a hosted API. It can be
  slow, it can be behind a caching plugin, and it can return an HTML error page
  from WordPress or from the host's WAF with a 200 status. So every response is
  checked for JSON before it is trusted.
* **Auth is a consumer key and secret**, sent as HTTP Basic over HTTPS. Sent over
  plain HTTP they are readable in transit, so http:// is refused outright rather
  than quietly downgraded.
* **Pagination is page numbers, not cursors.** Wordpress reports the totals in
  the `X-WP-Total` and `X-WP-TotalPages` headers. Page numbers can skip or repeat
  a record if the underlying set changes mid-walk, so imports are ordered by id
  ascending, which is stable, rather than by date.
* **There is no cost-based throttle.** Rate limiting, if any, comes from the host
  and shows up as a 429 or a 503, so backoff is the only sensible answer.
* **Errors are a JSON body with `code` and `message`**, not a userErrors array.
"""
import json
import logging
import re
import time

import requests

from odoo import _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

TIMEOUT = 45          # a merchant's own WordPress can be genuinely slow
MAX_ATTEMPTS = 5
RETRY_STATUS = {408, 429, 500, 502, 503, 504}
PER_PAGE = 50         # Woo's maximum is 100; half that survives slow hosts

# Consumer secrets look like the key, so both are scrubbed the same way.
SECRET_RE = re.compile(r'(ck_|cs_)[A-Za-z0-9]+')


def redact(text):
    """Never let a consumer key or secret reach a log or an error message."""
    if not text:
        return text
    return SECRET_RE.sub(lambda m: m.group(1) + '***', str(text))


class WooError(UserError):
    """A WooCommerce problem stated in terms the user can act on."""


class WooClient(object):
    """One authenticated conversation with one WooCommerce store."""

    def __init__(self, base_url, key, secret):
        self.base_url = (base_url or '').strip().rstrip('/')
        self.key = (key or '').strip()
        self.secret = (secret or '').strip()
        if not self.base_url or not self.key or not self.secret:
            raise WooError(_(
                "This store needs its address, consumer key and consumer secret. "
                "All three are on the WooCommerce tab of the store record."))
        if not self.base_url.startswith('https://'):
            # Basic auth over plain HTTP puts the credentials on the wire in
            # clear. Refused rather than downgraded, because a connector that
            # silently accepts http:// teaches people it is fine.
            raise WooError(_(
                "The store address must start with https://. WooCommerce sends "
                "the consumer key and secret with every request, and over plain "
                "HTTP anyone on the network can read them."))
        self._session = requests.Session()
        self._session.auth = (self.key, self.secret)

    @property
    def api(self):
        return self.base_url + '/wp-json/wc/v3'

    # ------------------------------------------------------------------
    def call(self, method, path, params=None, payload=None):
        """One REST call. Returns (body, headers). Raises WooError on failure."""
        url = '%s/%s' % (self.api, path.lstrip('/'))
        last = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self._session.request(
                    method, url, params=params or {},
                    json=payload if payload is not None else None,
                    timeout=TIMEOUT,
                    headers={'Accept': 'application/json'})
            except requests.exceptions.Timeout:
                last = _("The store did not respond within %s seconds.") % TIMEOUT
            except requests.exceptions.SSLError as err:
                raise WooError(_(
                    "The store's HTTPS certificate could not be verified: %s\n\n"
                    "Fix the certificate on the WooCommerce site. Ignoring this "
                    "would mean sending the consumer secret to whoever answered.")
                    % redact(err))
            except requests.exceptions.RequestException as err:
                last = _("Could not reach the store: %s") % redact(err)
            else:
                fatal = self._fatal(response)
                if fatal:
                    raise WooError(fatal)
                if response.status_code in RETRY_STATUS:
                    last = _("The store returned HTTP %s.") % response.status_code
                    self._sleep(response, attempt)
                    continue

                body = self._decode(response)
                # Woo reports failures as a JSON object with code and message,
                # sometimes alongside a 200 from an over-helpful proxy.
                if isinstance(body, dict) and body.get('code') and body.get('message'):
                    raise WooError(self._explain(body))
                return body, response.headers

            time.sleep(min(2 ** attempt, 20))

        raise WooError(_(
            "The store could not be reached after %(n)s attempts. Last problem: "
            "%(err)s") % {'n': MAX_ATTEMPTS, 'err': last})

    def _decode(self, response):
        try:
            return response.json()
        except ValueError:
            snippet = (response.text or '')[:200].strip().replace('\n', ' ')
            raise WooError(_(
                "The store returned something that is not JSON (HTTP %(code)s).\n\n"
                "This is almost always WordPress itself, a caching plugin or a "
                "firewall answering instead of the REST API. Check that "
                "%(api)s opens in a browser.\n\nWhat came back: %(snippet)s") % {
                    'code': response.status_code, 'api': self.api,
                    'snippet': snippet or '(empty)'})

    def _fatal(self, response):
        """Statuses that retrying cannot fix."""
        if response.status_code in (401, 403):
            return _(
                "WooCommerce rejected the consumer key and secret.\n\n"
                "Check them under WooCommerce, Settings, Advanced, REST API, and "
                "make sure the key has Read/Write permission. Some security "
                "plugins also strip the Authorization header: if the key is "
                "definitely right, that is the next thing to look at.")
        if response.status_code == 404:
            return _(
                "No WooCommerce REST API at %s.\n\n"
                "Check the address is the site root, and that permalinks are not "
                "set to Plain: the REST API needs pretty permalinks.") % self.api
        return None

    @staticmethod
    def _explain(body):
        code = body.get('code') or ''
        message = redact(body.get('message') or '')
        if 'woocommerce_rest_cannot_view' in code:
            return _("The consumer key does not have permission to read that. "
                     "Give it Read/Write in WooCommerce, Settings, Advanced, "
                     "REST API.")
        if 'woocommerce_rest_authentication_error' in code:
            return _("WooCommerce could not authenticate the request: %s") % message
        return _("WooCommerce refused the request: %(msg)s (%(code)s)") % {
            'msg': message, 'code': code}

    @staticmethod
    def _sleep(response, attempt):
        retry_after = response.headers.get('Retry-After')
        try:
            time.sleep(min(float(retry_after), 30.0))
        except (TypeError, ValueError):
            time.sleep(min(2 ** attempt, 20))

    # ------------------------------------------------------------------
    def paginate(self, path, params=None, max_pages=2000):
        """Walk a paged collection, yielding every record.

        Ordered by id ascending on purpose. Woo pages by number, so ordering by
        anything that changes while the walk is running (date_modified, say) lets
        a record shift between pages and be read twice or skipped entirely.
        """
        page = 1
        base = dict(params or {}, per_page=PER_PAGE, order='asc', orderby='id')
        while page <= max_pages:
            body, headers = self.call('GET', path, params=dict(base, page=page))
            if not isinstance(body, list):
                raise WooError(_(
                    "Expected a list of records from %s and got something else.") % path)
            for record in body:
                yield record
            total_pages = headers.get('X-WP-TotalPages')
            try:
                if page >= int(total_pages):
                    return
            except (TypeError, ValueError):
                if len(body) < PER_PAGE:
                    return
            page += 1
        _logger.warning(
            "miko_woocommerce: stopped paginating %s after %s pages; the sync is "
            "incomplete", path, max_pages)

    def system_status(self):
        """Identify the store. The cheapest proof the credentials work."""
        body, _headers = self.call('GET', 'system_status')
        env = (body or {}).get('environment') or {}
        settings = (body or {}).get('settings') or {}
        return {
            'name': env.get('site_url') or self.base_url,
            'wc_version': env.get('version'),
            'wp_version': env.get('wp_version'),
            'currency': settings.get('currency'),
        }

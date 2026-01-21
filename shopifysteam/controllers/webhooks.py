import base64
import hashlib
import hmac
import json
import logging
from datetime import datetime
from enum import Enum
from typing import Any, Dict

import pytz
from odoo import fields, http
from odoo.http import request

_logger = logging.getLogger(__name__)


class OrderStatus(Enum):
    FAILED = 'failed'
    SUCCESS = 'success'


class WebhookController(http.Controller):
    def _json_response(self, obj: Dict[str, Any], status: int):
        return request.make_response(json.dumps(
            obj), status=status)

    @http.route('/v1/webhooks/shopify/orders', type='http', auth='public', methods=['POST'], csrf=False)
    def shopify_order_created(self, **kwargs):
        """Toma el objeto Order enviado por Shopify y lo procesa para convertirlo en una orden en Odoo.

        params:
        self: instancia misma del objeto
        **kwargs:  cuerpo de la petición
        """
        raw_data = request.httprequest.data
        hmac_header = request.httprequest.headers.get('X-Shopify-Hmac-Sha256')
        if not self._verify_webhook(raw_data, hmac_header):
            _logger.warning("Unauthorized Shopify webhook attempt detected.")
            return self._json_response({'error': 'Unauthorized'}, status=401)
        try:
            data = json.loads(raw_data)
        except (json.JSONDecodeError, TypeError):
            _logger.error("Failed to decode JSON from Shopify webhook")
            return self._json_response({'status': OrderStatus.FAILED.value, 'error': 'Invalid JSON'}, status=400)
        if not data:
            return self._json_response({'status': OrderStatus.FAILED.value, 'error  ': 'Empty request body'}, 400)
        env = request.env['res.users'].sudo().env
        if data.get('financial_status') == 'voided':
            _logger.info("Ignoring Shopify Order %s: Status is VOIDED",
                         data.get('name'))
            return self._json_response({"status": OrderStatus.FAILED.value, "reason": "voided"}, 200)
        existing_order = env['sale.order'].search([
            ('client_order_ref', '=', str(data.get('id')))
        ], limit=1)

        if existing_order:
            return self._json_response({"status": OrderStatus.FAILED.value, "message": "Order already exists", "odoo_id": existing_order.id}, 200)
        try:
            shopify_customer = data.get('customer')
            partner_id = self._get_or_create_partner(env, shopify_customer)
            order_lines = []
            for item in data.get('line_items', []):
                product_id = self._get_or_create_product(env, item)
                order_lines.append(fields.Command.create({
                    'product_id': product_id,
                    'product_uom_qty': item.get('quantity'),
                    'price_unit': float(item.get('price', 0.0)),
                    'name': item.get('title'),
                }))

            created_at = data.get('created_at')

            if not created_at:
                order_date = fields.Datetime.now()
            else:
                dt = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                order_date = dt.astimezone(pytz.utc).replace(tzinfo=None)
            new_order = env['sale.order'].create({
                'partner_id': partner_id,
                'origin': data.get('name'),  # e.g. #9999
                'client_order_ref': str(data.get('id')),  # Shopify Internal ID
                'order_line': order_lines,
                'date_order': order_date,
            })
            new_order.action_confirm()
            invoice = new_order._create_invoices(final=True)
            if invoice:
                invoice.action_post()
                env.cr.commit()
            self._send_new_order_email(new_order)
            return self._json_response({"status": OrderStatus.SUCCESS.value, "odoo_id": new_order.id}, 200)

        except Exception as e:
            _logger.error("Shopify Sync Error: %s", str(e))
            env.cr.rollback()
            return self._json_response({"status": OrderStatus.FAILED.value, "message": str(e)}, 500)

    def _get_or_create_partner(self, env, shopify_cust):
        partner = env['res.partner'].search([
            '|',
            ('email', '=', shopify_cust.get('email')),
            ('ref', '=', str(shopify_cust.get('id')))
        ], limit=1)

        if not partner:
            partner = env['res.partner'].create({
                'name': f"{shopify_cust.get('first_name', '')} {shopify_cust.get('last_name', '')}",
                'email': shopify_cust.get('email'),
                'phone': shopify_cust.get('phone'),
                # Using 'ref' to store Shopify ID
                'ref': str(shopify_cust.get('id')),
            })
        return partner.id

    def _get_or_create_product(self, env, item):
        sku = item.get('sku')
        product = env['product.product'].search(
            [('default_code', '=', sku)], limit=1)
        if not product:
            product = env['product.product'].create({
                'name': item.get('title'),
                'default_code': sku,
                'list_price': float(item.get('price', 0.0)),
                'type': 'consu',
            })
        return product.id

    def _verify_webhook(self, data, hmac_header):
        """Standard Shopify HMAC verification logic"""
        if not hmac_header:
            return False
        shopify_secret = self.env['ir.config_parameter'].sudo(
        ).get_param('shopify.api_secret')
        digest = hmac.new(
            shopify_secret.encode('utf-8'),
            data,
            hashlib.sha256
        ).digest()

        computed_hmac = base64.b64encode(digest).decode()
        return hmac.compare_digest(computed_hmac, hmac_header)

    def _send_new_order_email(self, sale_order):
        """Function to send email with custom link"""
        base_url = "http://example.com"  # Replace with actual base URL retrieval logic
        custom_link = "%s/my/orders/%s" % (base_url, sale_order.id)
        template = self.env.ref('shopifysteam.new_sale_order_emailv1').sudo()
        template.with_context(
            custom_link=custom_link,
            special_note='Su pedido será enviado en 24 horas',
            tracking_number='TRK-%s' % sale_order.name,
            default_email_from="megalabs@steamsolutions.tech"
        ).sudo().send_mail(sale_order.id, force_send=True)

        return True

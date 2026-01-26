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
from odoo.exceptions import UserError
from odoo.http import request
from odoo.tools import format_amount, format_date

_logger = logging.getLogger(__name__)


class OrderStatus(Enum):
    FAILED = 'failed'
    SUCCESS = 'success'


class WebhookController(http.Controller):
    def _json_response(self, obj: Dict[str, Any], status: int):
        return request.make_response(json.dumps(
            obj), status=status)

    @http.route('/payment/success/<int:order_id>', type='http', auth='public')
    def payment_success(self, order_id):
        order = request.env['sale.order'].sudo().browse(order_id)
        if not order.exists():
            return request.not_found()
        formatted_date = format_date(request.env, order.date_order)
        formatted_total = format_amount(
            request.env, order.amount_total, order.currency_id)
        order_lines = [{
            'name': line.product_id.name,
            'qty': line.product_uom_qty,
            'subtotal': format_amount(request.env, line.price_subtotal, order.currency_id)
        } for line in order.order_line]
        return request.render('shopifysteam.succesful_payment', {'object': order,
                                                                 'formatted_date': formatted_date,
                                                                 'formatted_total': formatted_total,
                                                                 'order_lines': order_lines, })

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
            return self._json_response({'status': OrderStatus.FAILED.value, 'error': 'Invalid JSON'}, status=200)
        if not data:
            return self._json_response({'status': OrderStatus.FAILED.value, 'error  ': 'Empty request body'}, 200)
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
            partner_id = self._get_or_create_partner(
                env, shopify_customer, data)
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
                'company_id': env.company.id,
            })
            merchant_id = new_order.company_id.mercantil_merchant_id
            if not merchant_id:
                raise UserError(
                    "Mercantil Merchant ID not configured for company %s" % new_order.company_id.name)
            new_order.action_confirm()
            invoice = new_order._create_invoices(final=True)
            base_url = self.env['ir.config_parameter'].sudo(
            ).get_param('web.base.url')
            if invoice:
                invoice.action_post()
                env.cr.commit()
            mercantil_payment = env['sale.order.pago.mercantil'].create({
                'order_id': new_order.id,
                'merchant_id': new_order.company_id.mercantil_merchant_id,
                'return_url': f"https://www.steamsolutions.tech/",
                'invoice_number': new_order.client_order_ref or new_order.name,
                'invoice_creation_date': new_order.date_order.date() if new_order.date_order else fields.Date.today(),
                'invoice_cancelled_date': new_order.date_order.date() if new_order.date_order else fields.Date.today(),
                'contract_number': new_order.id,
                'contract_date': new_order.date_order.date() if new_order.date_order else fields.Date.today(),
                'trx_type': 'compra'
            })
            _logger.info(f"{mercantil_payment._build_transaction_data()}")
            mercantil_payment = env['sale.order.pago.mercantil'].search(
                [('order_id', '=', new_order.id)], limit=1)
            payment_link = mercantil_payment.generate_link_payment()
            self._send_new_order_email(new_order, payment_link)
            return self._json_response({"status": OrderStatus.SUCCESS.value, "odoo_id": new_order.id}, 200)

        except Exception as e:
            _logger.error("Shopify Sync Error: %s", str(e))
            env.cr.rollback()
            return self._json_response({"status": OrderStatus.FAILED.value, "message": str(e)}, 500)

    def _get_or_create_partner(self, env, shopify_cust, request):
        data = request.get("shipping_address")
        phone = data.get("phone") if data else shopify_cust.get(
            'billing_address', {}).get('phone')

        partner = env['res.partner'].search([
            '|',
            ('email', '=', shopify_cust.get('email')),
            ('ref', '=', str(shopify_cust.get('id')))
        ], limit=1)

        if not partner:
            partner = env['res.partner'].create({
                'name': f"{shopify_cust.get('first_name', '')} {shopify_cust.get('last_name', '')}".strip(),
                'email': shopify_cust.get('email'),
                'phone': phone,
                'ref': str(shopify_cust.get('id')),
            })
            billing = request.get('billing_address', {})
            if billing:
                country = env['res.country'].search(
                    [('code', '=', billing.get('country_code'))], limit=1)
                state = env['res.country.state'].search([
                    ('name', '=', billing.get('province')),
                    ('country_id', '=', country.id)
                ], limit=1) if country else None

                partner.write({
                    'street': billing.get('address1'),
                    'street2': billing.get('address2'),  # don't hardcode None
                    'city': billing.get('city'),
                    'zip': billing.get('zip'),
                    'state_id': state.id if state else False,
                    'country_id': country.id if country else False,
                    # optional: preserve billing phone
                    'phone': billing.get('phone') or phone,
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

    def _send_new_order_email(self, sale_order, custom_link):
        """Function to send email with custom link"""
        template = self.env.ref('shopifysteam.new_sale_order_emailv1').sudo()
        template.with_context(
            custom_link=custom_link,
            special_note='Su pedido será enviado en 24 horas',
            tracking_number='TRK-%s' % sale_order.name,
            default_email_from="megalabs@steamsolutions.tech"
        ).sudo().send_mail(sale_order.id, force_send=True)

        return True

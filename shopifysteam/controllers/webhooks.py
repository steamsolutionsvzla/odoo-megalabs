import logging
import pytz
from odoo import http, fields
from odoo.http import request
from datetime import datetime

_logger = logging.getLogger(__name__)


class WebhookController(http.Controller):

    @http.route('/v1/webhooks/shopify/orders', type='json', auth='public', methods=['POST'], csrf=False)
    def shopify_order_created(self, **kwargs):
        data = request.get_json_data()
        env = request.env['res.users'].sudo().env
        if data.get('financial_status') == 'voided':
            _logger.info("Ignoring Shopify Order %s: Status is VOIDED",
                         data.get('name'))
            return {"status": "ignored", "reason": "voided"}
        existing_order = env['sale.order'].search([
            ('client_order_ref', '=', str(data.get('id')))
        ], limit=1)

        if existing_order:
            return {"status": "success", "message": "Order already exists", "odoo_id": existing_order.id}
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
                'date_order': order_date
            })
            if data.get('financial_status') in ['paid', 'authorized']:
                new_order.action_confirm()
            return {"status": "success", "odoo_id": new_order.id}

        except Exception as e:
            _logger.error("Shopify Sync Error: %s", str(e))
            return {"status": "error", "message": str(e)}

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

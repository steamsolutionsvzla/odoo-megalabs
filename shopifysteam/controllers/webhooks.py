import base64
import hashlib
import hmac
import json
import logging
from datetime import datetime
from enum import Enum
from typing import Any, Dict

import pytz
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
from odoo import fields, http
from odoo.exceptions import UserError
from odoo.http import request
from odoo.tools import format_amount, format_date

_logger = logging.getLogger(__name__)


PAYMENT_MAPPING = {
    'Pago Móvil': 'shopifysteam.pm_mobile_payment',
    'Transferencia Bancaria': 'shopifysteam.pm_bank_transfer',
    'Zelle': 'shopifysteam.pm_zelle'
}


class WebhookController(http.Controller):
    def _json_response(self, obj: Dict[str, Any], status: int):
        return request.make_response(json.dumps(
            obj), status=status)

    @http.route('/payment/processing', auth='public', website=True)
    def payment_processing(self, **kwargs):
        return request.render('shopifysteam.payment_processing_page')

    @http.route('/v1/webhooks/mercantil/payment/confirmation', type='http', auth='public', csrf=False)
    def mercantil_confirm_payment(self, **kwargs):
        raw_data = request.httprequest.data
        try:
            data = json.loads(raw_data)
            _logger.info(f"Received encrypted webhook data: {data}")
            encrypted_data = data.get('data')
            if not encrypted_data:
                _logger.error("No 'data' field found in webhook")
                # Escribir formato de error
                return self._json_response({}, 200)
            secret_key = request.env['ir.config_parameter'].sudo(
            ).get_param('pago_mercantil.secret_key')
            if not secret_key:
                _logger.error("Mercantil secret key not configured")
                # Escribir formato de error
                return self._json_response({}, 200)
            decrypted_data = self._decrypt_mercantil_data(
                encrypted_data, secret_key)
            if not decrypted_data:
                _logger.error("Failed to decrypt webhook data")
                # Escribir formato de error
                return self._json_response({}, 400)
            _logger.info(f"Decrypted webhook data: {decrypted_data}")
            # Extract webhook notification data
            webhook_notification = decrypted_data.get(
                'webhookNotificationIn', {})
            info_msg = decrypted_data.get('infoMsg', {})
            numero_factura = webhook_notification.get('numeroFactura')
            guid = info_msg.get('guId')
            if not numero_factura:
                _logger.error("No numeroFactura found in decrypted data")
                # Escribir formato de error
                return self._json_response({}, 400)
            PagoMercantil = request.env['sale.order.pago.mercantil'].sudo()
            pago_record = PagoMercantil.search(
                [('invoice_number', '=', numero_factura)], limit=1)

            if not pago_record:
                _logger.warning(f"Invoice number {numero_factura} not found")
                return self._json_response({
                    "status": "error",
                    "message": "Invoice doesn't exist",
                    "numeroFactura": numero_factura
                }, 200)
            if pago_record.webhook_response:
                try:
                    existing_data = json.loads(pago_record.webhook_response)
                    existing_guid = existing_data.get(
                        'infoMsg', {}).get('guId')
                    if existing_guid == guid:
                        _logger.info(
                            f"Duplicate webhook detected for invoice {numero_factura}, guId: {guid}")
                        response = self._build_mercantil_response(
                            info_msg, 0, "06", "Notificación duplicada",
                            "Webhook already processed", guid
                        )
                        return self._json_response(response, 200)
                except json.JSONDecodeError:
                    _logger.warning(
                        f"Failed to decode existing webhook_response for invoice {numero_factura}")
            invoice = request.env['account.move'].sudo().search([
                ('ref', '=', numero_factura),
                ('payment_state', 'not in', ['paid', 'in_payment'])
            ], limit=1)
            if invoice:
                payment_register = request.env['account.payment.register'].sudo().with_context(
                    active_model='account.move',
                    active_ids=invoice.ids
                ).create({
                    'journal_id': request.env['account.journal'].sudo().search([('type', '=', 'bank')], limit=1).id,
                })
                payment_register.action_create_payments()

                _logger.info(
                    f"Invoice {numero_factura} marked as paid via Mercantil webhook.")
            else:
                _logger.error(
                    f"Invoice {numero_factura} found in custom logs but not in account.move or already paid.")
            pago_record.write({
                'webhook_response': json.dumps(decrypted_data, ensure_ascii=False)
            })
            _logger.info(
                f"Webhook response saved for invoice {numero_factura}")
            response = self._build_mercantil_response(
                info_msg, 0, "00", "Notificacion recibida con éxito!", "Notificacion recibida con éxito!!", guid
            )
            _logger.info(response)
            return self._json_response(response, 200)

        except json.JSONDecodeError as e:
            _logger.error(
                f"Failed to decode JSON from Mercantil webhook: {str(e)}")
            return self._json_response({"error": "Invalid JSON"}, 400)
        except Exception as e:
            _logger.error(
                f"Unexpected error processing Mercantil webhook: {str(e)}")
            return self._json_response({"error": "Internal server error"}, 500)

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
            return self._json_response({'message': 'Unauthorized'}, status=401)
        try:
            data = json.loads(raw_data)
        except (json.JSONDecodeError, TypeError):
            _logger.error("Failed to decode JSON from Shopify webhook")
            return self._json_response({'message': 'Invalid JSON'}, status=200)
        if not data:
            return self._json_response({'message': 'Empty request body'}, 200)
        env = request.env['res.users'].sudo().env
        if data.get('financial_status') == 'voided':
            _logger.info("Ignoring Shopify Order %s: Status is VOIDED",
                         data.get('name'))
            return self._json_response({"reason": "voided"}, 200)
        existing_order = env['sale.order'].search([
            ('client_order_ref', '=', str(data.get('id')))
        ], limit=1)

        if existing_order:
            return self._json_response({"message": "Order already exists", "odoo_id": existing_order.id}, 200)
        try:
            shopify_customer = data.get('customer')
            partner_id = self._get_or_create_partner(
                env, shopify_customer, data)
            partner = env['res.partner'].search([
                ('id', '=', partner_id)
            ], limit=1)
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
            default_pm = env.ref('shopifysteam.pm_mobile_payment').id
            shipping_data = data.get('shipping_lines', [])
            shipping_name = shipping_data[0].get(
                'title') if shipping_data else 'No Shipping'
            delivery_method = env['sale.delivery.method'].search(
                [('name', '=', shipping_name.strip().lower().replace(' ', '_'))], limit=1)
            default_dm = env.ref('shopifysteam.dm_standard').id
            gateways = data.get('payment_gateway_names', [])
            payment_name = gateways[0] if gateways else 'unknown'
            target_xml_id = PAYMENT_MAPPING.get(payment_name.strip().lower().replace(
                ' ', '_'), 'shopifysteam.pm_mobile_payment')
            payment_method_id = env.ref(target_xml_id).id
            default_pm = env.ref('shopifysteam.pm_mobile_payment').id
            billing_data = self._get_billing_address(env, data)
            _logger.info(billing_data)
            note_content = (
                f"--- INFORMACIÓN DE DESPACHO ---\n"
                f"Método de Envío: {default_dm}\n"
                f"Pasarela de Pago: {default_pm}\n\n"
                f"--- DIRECCIÓN DE FACTURACIÓN ---\n"
                f"{partner.name}\n"
                f"{billing_data.get('street')}, {billing_data.get('street2') or ''}\n"
                f"{billing_data.get('city')}, {billing_data.get('province') or ''} {billing_data.get('zip') or ''}\n"
                f"Tel: {billing_data.get('phone') or partner.phone or 'N/A'}\n\n"
                f"--- NOTAS ADICIONALES ---\n"
                f"Por favor, si su pago es por transferencia o Pago Móvil, "
                f"envíe el comprobante al correo de contacto."
            )
            new_order = env['sale.order'].create({
                'partner_id': partner_id,
                'origin': data.get('name'),  # e.g. #9999
                'client_order_ref': str(data.get('id')),  # Shopify Internal ID
                'order_line': order_lines,
                'date_order': order_date,
                'company_id': env.company.id,
                'delivery_method_id': delivery_method or default_dm,
                'payment_method_id': payment_method_id or default_pm,
                'note': note_content
            })
            shopify_status = data.get('financial_status')

            if shopify_status == 'paid':
                new_order.action_confirm()
                invoice = new_order._create_invoices(final=True)
                invoice.action_post()
                journal = env['account.journal'].search(
                    [('code', '=', 'BNK1')], limit=1)

                if not journal:
                    _logger.error("Bank Journal with code 'BNK1' not found!")
                    journal = env['account.journal'].search(
                        [('type', '=', 'bank')], limit=1)
                payment = env['account.payment'].create({
                    'amount': invoice.amount_total,
                    'payment_type': 'inbound',
                    'partner_type': 'customer',
                    'journal_id': journal.id,
                    'partner_id': partner_id,
                    'memo': f"Shopify {data.get('name')}",
                })
                payment.action_post()
                # (payment.move_id.line_ids + invoice.line_ids).filtered(
                #     lambda l: l.account_id.account_type == 'asset_receivable' and not l.reconciled
                # ).reconcile()
                return self._json_response({"message": "Order Created and Paid"}, 200)
            elif shopify_status in ['voided', 'refunded']:
                new_order.action_cancel()
                return self._json_response({"message": "Order Created and Cancelled"}, 200)
            else:
                new_order.action_confirm()
                invoice = new_order._create_invoices(final=True)
                invoice.action_post()
                merchant_id = new_order.company_id.mercantil_merchant_id
                if not merchant_id:
                    _logger.error(
                        "Mercantil Merchant ID not configured for company %s", new_order.company_id.name)
                    return self._json_response({"message": "Merchant ID missing"}, 200)
                mercantil_payment = env['sale.order.pago.mercantil'].create({
                    'order_id': new_order.id,
                    'merchant_id': merchant_id,
                    'return_url': "https://megalabs.steamsolutions.tech/payment/processing",
                    'invoice_number': new_order.client_order_ref or new_order.name,
                    'invoice_creation_date': new_order.date_order.date() if new_order.date_order else fields.Date.today(),
                    'invoice_cancelled_date': new_order.date_order.date() if new_order.date_order else fields.Date.today(),
                    'contract_number': new_order.id,
                    'contract_date': new_order.date_order.date() if new_order.date_order else fields.Date.today(),
                    'trx_type': 'compra'
                })
                payment_link = mercantil_payment.generate_link_payment()
                self._send_new_order_email(new_order, payment_link)
                return self._json_response({"message": "Order Draft Created, Link Sent"}, 200)

        except Exception as e:
            _logger.error("Shopify Sync Error: %s", str(e))
            env.cr.rollback()
            return self._json_response({"message": "error"}, 500)

    def _get_billing_address(self, env, request: Dict):
        billing = request.get("billing_address") or {}
        country = env['res.country'].search(
            [('code', '=', billing.get('country_code'))], limit=1)
        state = env['res.country.state'].search([
            ('name', '=', billing.get('province')),
            ('country_id', '=', country.id)
        ], limit=1) if country else None
        return {
            'street': billing.get('address1'),
            'street2': billing.get('address2'),
            'city': billing.get('city'),
            'zip': billing.get('zip'),
            'state_id': state.id if state else False,
            'country_id': country.id if country else False,
            'phone': billing.get('phone'),
        }

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
        
        shopify_secret = self.env['ir.config_parameter'].sudo( # pyright: ignore[reportOptionalSubscript]
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
        template = self.env.ref('shopifysteam.new_sale_order_emailv1').sudo()  # pyright: ignore[reportOptionalMemberAccess]
        _logger.info(custom_link)
        template.with_context(
            custom_link=custom_link,
            special_note='Su pedido será enviado en 24 horas',
            tracking_number='TRK-%s' % sale_order.name,
            default_email_from="megalabs@steamsolutions.tech"
        ).sudo().send_mail(sale_order.id, force_send=True)

        return True

    def _decrypt_mercantil_data(self, encrypted_data, secret_key):
        """
        Descifra datos que fueron encriptados utilizando el modo AES ECB.

        Args:
            encrypted_data (str): Cadena encriptada codificada en Base64.
            secret_key (str): La clave secreta para el descifrado.

        Returns:
            dict: Los datos JSON descifrados como un diccionario, o None si ocurre un error.
        """
        try:
            # Generar el mismo hash de clave utilizado para la encriptación
            key_hash = hashlib.sha256(secret_key.encode('utf-8')).digest()[:16]

            # Decodificar base64
            encrypted_bytes = base64.b64decode(encrypted_data)

            # Descifrar usando modo AES ECB
            cipher = AES.new(key_hash, AES.MODE_ECB)
            decrypted_padded = cipher.decrypt(encrypted_bytes)

            # Eliminar el relleno (padding)
            decrypted = unpad(decrypted_padded, AES.block_size)

            # Convertir a JSON
            json_str = decrypted.decode('utf-8')
            return json.loads(json_str)
        except Exception as e:
            _logger.error(f"Error descifrando datos de Mercantil: {str(e)}")
            return None

    def _build_mercantil_response(self: object, info_msg: Dict, code: int, codigo: str, mensaje_cliente: str, mensaje_sistema: str, id_registro: str = ""):
        """
        Construye una respuesta estandarizada para el servicio de Mercantil.

        Args:
            info_msg (dict): Diccionario que contiene metadatos de la cabecera (guId, canal, etc.).
            code (int): Código de respuesta general.
            codigo (str): Código específico de la operación.
            mensaje_cliente (str): Mensaje amigable destinado al usuario final.
            mensaje_sistema (str): Detalle técnico del mensaje para fines de depuración.
            id_registro (str, opcional): Identificador único del registro procesado.

        Returns:
            dict: Estructura de respuesta formateada según los requerimientos de Mercantil.
        """
        return {
            "infoMsg": {
                "guId": info_msg.get('guId', ''),
                "channel": info_msg.get('channel', ''),
                "subchannel": info_msg.get('subchannel', ''),
                "applId": info_msg.get('applId', ''),
                "personId": info_msg.get('personId', ''),
                "userId": info_msg.get('userId', ''),
                "token": info_msg.get('token', ''),
                "action": info_msg.get('action', '')
            },
            "code": code,
            "codigo": codigo,
            "mensajeCliente": mensaje_cliente,
            "mensajeSistema": mensaje_sistema,
            "idRegistro": id_registro
        }

import base64
import hashlib
import json

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from odoo import api, fields, models
from odoo.exceptions import UserError


class PagoMercantil(models.Model):
    _name = 'sale.order.pago.mercantil'
    _description = 'Mercantil Transaction Data for Sale Order'
    _inherit = ['mail.thread', 'mail.activity.mixin']

    order_id = fields.Many2one(
        'sale.order', string='Sale Order', required=True, ondelete='restrict')
    amount = fields.Monetary(
        string='Amount', related='order_id.amount_total', store=True)
    customer_name = fields.Char(
        string='Customer Name', compute='_compute_customer_name', store=True)
    merchant_id = fields.Char(
        string='Merchant ID', default='', required=True)
    return_url = fields.Char(string='Return URL')
    trx_type = fields.Selection([
        ('compra', 'Compra'),
        ('venta', 'Venta')
    ], string='Transaction Type', default='compra')
    currency = fields.Char(string='Currency', default='ves', required=True)
    payment_concepts = fields.Char(
        string='Payment Concepts (JSON)',
        default='["b2b","c2p","tdd"]',
        help='Store as JSON string'
    )
    invoice_number = fields.Char(string='Invoice Number', required=True)
    invoice_creation_date = fields.Date(
        string='Invoice Creation Date', required=True)
    invoice_cancelled_date = fields.Date(string='Invoice Cancelled Date')
    contract_number = fields.Char(string='Contract Number', required=True)
    contract_date = fields.Date(string='Contract Date',)

    currency_id = fields.Many2one(
        'res.currency', related='order_id.currency_id', required=True)

    def generate_link_payment(self):
        mercantil_payment_url = self._get_config_key('mercantil_payment_url')
        merchant_id = self.merchant_id
        integrator_id = self._get_config_key('integrator_id')
        custom_link = f"{mercantil_payment_url}/?merchantid={merchant_id}&transactiondata={self._encrypt_transaction_data()}&integratorid={integrator_id}"
        return custom_link

    def _build_transaction_data(self):
        """Build dict for bank encryption"""
        self.ensure_one()
        return {
            "amount": self.amount,
            "customerName": self.customer_name,
            "returnUrl": self.return_url,
            "merchantId": self.merchant_id,
            "invoiceNumber": {
                "number": self.invoice_number,
                "invoiceCreationDate": self.invoice_creation_date.strftime("%Y-%m-%d") if self.invoice_creation_date else "",
                "invoiceCancelledDate": self.invoice_cancelled_date.strftime("%Y-%m-%d") if self.invoice_cancelled_date else ""
            },
            "contract": {
                "contractNumber": self.contract_number,
                "contractDate": self.contract_date.strftime("%Y-%m-%d") if self.contract_date else ""
            },
            "trxType": self.trx_type,
            "currency": self.currency,
            "paymentConcepts": eval(self.payment_concepts) if self.payment_concepts else []
        }

    @api.depends('order_id.partner_id')
    def _compute_customer_name(self):
        for rec in self:
            rec.customer_name = rec.order_id.partner_id.name

    def _get_config_key(self, config_key: str):
        key = self.env['ir.config_parameter'].sudo().get_param(
            f'pago_mercantil.{config_key}'
        )
        if not key:
            raise UserError(f"Missing {config_key} for mercantil payment")
        if isinstance(key, tuple):
            key = key[0] if key else ""
        key = str(key).strip()

        if not key:
            raise UserError(f"{config_key} is empty")
        return key

    def _encrypt_transaction_data(self):
        transaction_data = self._build_transaction_data()
        key = self._get_config_key('secret_key')
        key_hash = hashlib.sha256(key.encode(
            'utf-8')).digest()[:16]
        json_str = json.dumps(transaction_data, ensure_ascii=False)
        cipher = AES.new(key_hash, AES.MODE_ECB)
        encrypted = cipher.encrypt(
            pad(json_str.encode('utf-8'), AES.block_size))
        return base64.b64encode(encrypted).decode('utf-8')

from odoo import models, fields

class PaymentMethod(models.Model):
    _name = 'sale.payment.method'
    _description = 'Payment Method'

    name = fields.Char('Name', required=True)
    active = fields.Boolean(default=True)
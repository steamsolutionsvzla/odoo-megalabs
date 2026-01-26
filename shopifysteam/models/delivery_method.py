from odoo import models, fields

class DeliveryMethod(models.Model):
    _name = 'sale.delivery.method'
    _description = 'Delivery Method'

    name = fields.Char('Name', required=True, translate=True)
    active = fields.Boolean(default=True)
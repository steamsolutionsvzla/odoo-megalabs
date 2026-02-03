from odoo import models, fields

class SaleOrder(models.Model):
    _inherit = 'sale.order'

    delivery_method_id = fields.Many2one(
        'sale.delivery.method',
        string='Delivery Method'
    )
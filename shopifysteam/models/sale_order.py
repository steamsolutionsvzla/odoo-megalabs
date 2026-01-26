from odoo import models, fields

class SaleOrder(models.Model):
    _inherit = 'sale.order'

    payment_method_id = fields.Many2one(
        'sale.payment.method',
        string='Payment Method'
    )
    delivery_method_id = fields.Many2one(
        'sale.delivery.method',
        string='Delivery Method'
    )
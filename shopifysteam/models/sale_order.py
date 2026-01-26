from odoo import models, fields

class SaleOrder(models.Model):
    _inherit = 'sale.order'

    payment_method_id = fields.Many2one(
        'sale.payment.method',
        string='Payment Method'
    )
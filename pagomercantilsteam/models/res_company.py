from odoo import models, fields


class ResCompany(models.Model):
    _inherit = 'res.company'

    mercantil_merchant_id = fields.Char(string='Mercantil Merchant ID')

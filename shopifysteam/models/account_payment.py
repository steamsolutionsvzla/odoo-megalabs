from odoo import fields, models


class AccountPayment(models.Model):
    _inherit = 'account.payment'
    payment_method_id = fields.Many2one(
        'account.payment.method',
        string='Payment Method'
    )
    mercantil_payment = fields.Many2one(
        'sale.order.pago.mercantil', 'Mercantil Payment')

    amount_ves = fields.Monetary(
        related='mercantil_payment.amount_ves',
        string='Amount VES',
        readonly=True,
        store=True,
        currency_field='currency_id'
    )
    fixed_exchange_rate = fields.Float(
        related='mercantil_payment.fixed_exchange_rate',
        string='Fixed Exchange Rate',
        readonly=True,
        store=True,
    )


class AccountPaymentRegister(models.TransientModel):
    _inherit = 'account.payment.register'

    payment_method_id = fields.Many2one(
        'account.payment.method',
        string='Payment Method'
    )
    mercantil_payment = fields.Many2one(
        'sale.order.pago.mercantil', 'Mercantil Payment')

    def _create_payment_vals_from_wizard(self, batch_result):
        vals = super()._create_payment_vals_from_wizard(batch_result)
        # .id is correct here, but ensure the search in the controller matches this model
        vals['payment_method_id'] = self.payment_method_id.id
        vals['mercantil_payment'] = self.mercantil_payment.id
        return vals

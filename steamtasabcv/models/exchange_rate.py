import logging

import requests
import urllib3
from bs4 import BeautifulSoup
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_logger = logging.getLogger(__name__)


class ExchangeRate(models.Model):
    _name = 'steamtasabcv.exchange.rate'
    _description = 'Exchange Rate from BCV'
    _order = 'name desc, rate desc'

    name = fields.Date(
        string='Date',
        required=True,
        default=fields.Date.today,
        help="The date for which this exchange rate is valid."
    )

    currency_id = fields.Many2one(
        'res.currency',
        string='Currency',
        required=True,
        help="The foreign currency to exchange (e.g., USD, EUR)."
    )

    company_id = fields.Many2one(
        'res.company',
        string='Company',
        required=True,
        default=lambda self: self.env.company
    )

    rate = fields.Float(
        string='Exchange Rate',
        digits=(12, 6),
        required=True,
        help="The rate to convert from the company currency to this currency."
    )

    inverse_rate = fields.Float(
        string='Inverse Rate',
        compute='_compute_inverse_rate',
        inverse='_set_inverse_rate',
        digits=(12, 6),
        store=True,
        help="The rate to convert from this currency back to the company currency (1/rate)."
    )

    active = fields.Boolean(default=True)

    _sql_constraints = [
        ('unique_currency_per_day', 'UNIQUE(name, currency_id, company_id)',
         'Only one exchange rate per currency per day is allowed!')
    ]

    @api.depends('rate')
    def _compute_inverse_rate(self):
        for record in self:
            if record.rate and record.rate != 0:
                record.inverse_rate = 1.0 / record.rate
            else:
                record.inverse_rate = 0.0

    def _set_inverse_rate(self):
        for record in self:
            if record.inverse_rate and record.inverse_rate != 0:
                record.rate = 1.0 / record.inverse_rate
            else:
                record.rate = 0.0

    def action_update_currency_rate(self):
        """
        Push this BCV rate to the standard Odoo res.currency.rate table.
        """
        self.ensure_one()
        if not self.currency_id:
            raise ValidationError(_("Please select a currency first."))
        RateModel = self.env['res.currency.rate']
        existing_rate = RateModel.search([
            ('currency_id', '=', self.currency_id.id),
            ('name', '=', self.name),
            ('company_id', '=', self.company_id.id)
        ], limit=1)

        if existing_rate:
            existing_rate.rate = self.rate
            message = _('Currency rate updated successfully.')
        else:
            RateModel.create({
                'currency_id': self.currency_id.id,
                'name': self.name,
                'rate': self.rate,
                'company_id': self.company_id.id
            })
            message = _('Currency rate created successfully.')

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Success'),
                'message': message,
                'type': 'success',
                'sticky': False,
            }
        }

    @api.model
    def cron_fetch_bcv_rate(self):
        """
        Cron job to fetch the exchange rate from bcv.org.ve and update Odoo.
        """
        bcv_url = "https://www.bcv.org.ve/"
        rate_value = 0.0

        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            }
            response = requests.get(
                bcv_url, headers=headers, timeout=20, verify=False)

            if response.status_code == 200:
                soup = BeautifulSoup(response.content, 'html.parser')
                dolar_div = soup.find('div', id='dolar')

                if dolar_div:
                    strong_tag = dolar_div.find('strong')
                    if strong_tag:
                        # Clean data: "36,50" -> "36.50"
                        raw_text = strong_tag.get_text(strip=True)
                        clean_text = raw_text.replace(
                            ',', '.').replace(' ', '')
                        try:
                            rate_value = float(clean_text)
                            _logger.info(
                                f"BCV Scraper: Rate found: {rate_value}")
                        except ValueError:
                            _logger.error(
                                f"BCV Scraper: Could not convert '{clean_text}' to float.")
                    else:
                        _logger.error(
                            "BCV Scraper: <strong> tag not found inside #dolar.")
                else:
                    _logger.error("BCV Scraper: Div #dolar not found.")
            else:
                _logger.error(
                    f"BCV Scraper: HTTP Error {response.status_code}")

        except Exception as e:
            _logger.error(f"BCV Scraper: Exception: {e}")
            return
        if rate_value > 0:
            ves_currency = self.env['res.currency'].search(
                [('name', '=', 'VES')], limit=1)

            if not ves_currency:
                _logger.error(
                    "BCV Scraper: Currency 'VES' not found in Odoo configuration!")
                return

            today = fields.Date.today()
            existing_custom_rate = self.search([
                ('name', '=', today),
                ('currency_id', '=', ves_currency.id),
                ('company_id', '=', self.env.company.id)
            ], limit=1)

            vals = {
                'name': today,
                'currency_id': ves_currency.id,
                'rate': rate_value,
                'company_id': self.env.company.id
            }

            try:
                if existing_custom_rate:
                    existing_custom_rate.write(vals)
                    record_to_use = existing_custom_rate
                    _logger.info(
                        f"BCV Scraper: Updated local record for {today}")
                else:
                    record_to_use = self.create(vals)
                    _logger.info(
                        f"BCV Scraper: Created local record for {today}")
                record_to_use.action_update_currency_rate()
                _logger.info(
                    "BCV Scraper: Successfully pushed rate to Odoo Currency Table.")

            except Exception as e:
                _logger.error(f"BCV Scraper: Database write error: {e}")

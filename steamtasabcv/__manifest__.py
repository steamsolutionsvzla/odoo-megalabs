{
    "name": "BCV Exchange Rate Steam",
    "version": "19.0.0.1",
    "category": "Sales",
    "depends": ["base",],
    "description": """
    Odoo module to scrape the exchange rates from the BCV""",
    "author": "Carlos",
    "installable": True,
    "application": True,
    "auto_install": False,
    "license": "OPL-1",
    "data": [
        "views/exchange_rate_view.xml",
        "security/ir.model.access.csv",
    ],
}

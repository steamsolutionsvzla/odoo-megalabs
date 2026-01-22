{
    "name": "Shopify Steam",
    "version": "19.0.0.1",
    "category": "Sales",
    "depends": ["base", "sale_management", "mail", "pagomercantilsteam"],
    "description": """
    Odoo module which holds a deep integration with the E-Commerce platform Shopify. 
    Provides webhooks to connect with the Sales module from Odoo""",
    "author": "Carlos",
    "installable": True,
    "application": True,
    "auto_install": False,
    "license": "OPL-1",
    'data': [
        'data/new_sale_order_emailv1.xml',
        'data/succesful_payment.xml',
    ],
}

{
    "name": "Pago Mercantil Steam",
    "version": "19.0.0.1",
    "category": "Sales",
    "depends": ["base", "sale", "sale_management", "mail"],
    "description": """
    Odoo module which holds a deep integration with the bank named Mercantil.""",
    "author": "Carlos",
    "installable": True,
    "application": True,
    "auto_install": False,
    "license": "OPL-1",
    'data': [
        'views/res_company_views.xml',
        'views/pago_mercantil_views.xml',
        'security/ir.model.access.csv'
    ],
    
}

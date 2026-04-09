{
    "name": "Sales Credit Limit Control",
    "version": "1.0",
    "summary": "Check customer credit limit before confirming Sales Order",
    "category": "Sales",

    "depends": [
        "sale",
        "account",
    ],

    "data": [
        "security/ir.model.access.csv",
        "views/res_partner_views.xml",
        "views/stock_picking_views.xml",
        'reports/delivery_challan.xml',
        'views/sales_report_views.xml',
    ],

    "installable": True,
    "application": False,
    "license": "LGPL-3",
}

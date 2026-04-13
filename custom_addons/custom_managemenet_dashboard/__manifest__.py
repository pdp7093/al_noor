{
    'name': 'Al Noor Management Dashboard',
    'version': '1.0',
    'category': 'Reporting',
    'summary': 'Executive Dashboard for Production, Stock and Finance',
    'depends': ['base', 'mrp', 'stock', 'sale', 'account', 'stock_account'],
    'data': [
        'security/ir.model.access.csv',
        'views/management_dashboard_view.xml',
        'views/product_stock_view.xml',
        'data/dashboard_data.xml',
        'data/cron.xml',
     

    ],
    'assets': {
        'web.assets_backend': [
            'custom_managemenet_dashboard/static/src/scss/management_dashboard.scss',
        ],
    },
    'installable': True,
    'application': True,
}

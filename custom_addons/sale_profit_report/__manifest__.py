{
    'name': 'Sale Profit Report',
    'version': '1.0',
    'depends': ['sale', 'stock_account'],
    'data': [
        'views/sale_report_views.xml',
        'data/ir_cron_data.xml',
        'data/mail_template_data.xml',
    ],
    'installable': True,
}

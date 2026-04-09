{
    'name': 'Stock Alert',
    'version': '19.0.1.0.0',
    'summary': 'Send stock alert emails when products fall below reorder minimums',
    'author': 'Brainstream',
    'license': 'LGPL-3',
    'depends': ['stock', 'mail', 'hr','mrp','base'],
    'data': [
        'security/ir.model.access.csv',
        'data/mail_template.xml',
        'data/cron.xml',
    ],
    'installable': True,
}   

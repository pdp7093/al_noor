# Part of Odoo. See LICENSE file for full copyright and licensing details.

{
    'name': "Payment Provider: PayU",
    'version': '19.0.1.0',
    'category': 'Accounting/Payment Providers',
    'sequence': 350,
    'summary': "A payment provider covering India.",
    'website': "https://www.odoo.com/",
    'description': " ",  # Non-empty string to avoid loading the README file.,
    'depends': ['payment'],
    'images': ['static/description/banner.jpg'],
    'data': [
        'views/payment_provider_views.xml',
        'data/payment_provider_data.xml'
    ],
    'assets': {
        'web.assets_frontend': [
            'odoo_payment_payu/static/src/interactions/payment_form.js'
        ],
    },
    'post_init_hook': 'post_init_hook',
    'uninstall_hook': 'uninstall_hook',
    'author': "Odoo IN Pvt Ltd",
    'license': 'LGPL-3',
}
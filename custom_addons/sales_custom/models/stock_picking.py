from odoo import   models, fields

class StockPicking(models.Model):
    _inherit = 'stock.picking'

    driver_name = fields.Char(string="Driver Name")
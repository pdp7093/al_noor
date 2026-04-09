from odoo import models, fields, api

class ProductProduct(models.Model):
    _inherit = 'product.product'

    stock_status = fields.Selection([
        ('red', 'Critical (Below Min)'),
        ('yellow', 'Warning (Near Min)'),
        ('green', 'Safe (Above Min)')
    ], compute='_compute_stock_status', string="Stock Status")

    def _compute_stock_status(self):
        for product in self:
            orderpoint = self.env['stock.warehouse.orderpoint'].search([
                ('product_id', '=', product.id)], limit=1)
            min_qty = orderpoint.product_min_qty if orderpoint else 0
            
            if product.qty_available <= min_qty:
                product.stock_status = 'red'
            elif product.qty_available <= (min_qty * 1.5):
                product.stock_status = 'yellow'
            else:
                product.stock_status = 'green'
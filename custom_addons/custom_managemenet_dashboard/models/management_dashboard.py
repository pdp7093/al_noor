from odoo import models, fields, api
from datetime import date, datetime
from dateutil.relativedelta import relativedelta


class ManagementDashboard(models.Model):
    _name = 'management.dashboard'
    _description = 'Executive Management Dashboard'

    name = fields.Char(default="Executive Management Dashboard")

    production_today = fields.Integer(compute='_compute_stats')
    pending_deliveries = fields.Integer(compute='_compute_stats')
    outstanding_payments = fields.Char(compute='_compute_stats')

    stock_red = fields.Integer(compute='_compute_stats')
    stock_yellow = fields.Integer(compute='_compute_stats')
    stock_green = fields.Integer(compute='_compute_stats')

    top_products_html = fields.Html(compute='_compute_stats')

    def _compute_stats(self):
        today = date.today()
        month_start = today.replace(day=1)
        products = self.env['product.product'].search([
            ('type', '=', 'product')
        ])
        orderpoints = self.env['stock.warehouse.orderpoint'].search([
            ('product_id', 'in', products.ids)
        ])
        min_qty_by_product = {
            orderpoint.product_id.id: orderpoint.product_min_qty
            for orderpoint in orderpoints
        }

        for rec in self:
            # Stats calculation
            rec.production_today = self.env['mrp.production'].search_count([
                ('state', '=', 'done'),
                ('date_finished', '>=', today)
            ])

            rec.pending_deliveries = self.env['stock.picking'].search_count([
                ('picking_type_code', '=', 'outgoing'),
                ('state', 'in', ['assigned', 'confirmed'])
            ])

            invoices = self.env['account.move'].search([
                ('move_type', '=', 'out_invoice'),
                ('state', '=', 'posted'),
                ('payment_state', '!=', 'paid')
            ])

            rec.outstanding_payments = f" {sum(invoices.mapped('amount_residual')):,.2f}"

            # Stock Health
            rec.stock_red = len(products.filtered(
                lambda p: p.qty_available <= min_qty_by_product.get(p.id, 0)
            ))

            rec.stock_yellow = len(products.filtered(
                lambda p: min_qty_by_product.get(p.id, 0) < p.qty_available <= (min_qty_by_product.get(p.id, 0) * 1.5)
            ))

            rec.stock_green = len(products.filtered(
                lambda p: p.qty_available > (min_qty_by_product.get(p.id, 0) * 1.5)
            ))

            # Aggregate top-selling products from confirmed/completed sales this month.
            sales_data = self.env['sale.order.line']._read_group(
                [('order_id.state', 'in', ['sale', 'done']),
                 ('create_date', '>=', month_start)],
                ['product_id'],
                ['product_uom_qty:sum'],
                limit=5,
                order='product_uom_qty:sum desc'
            )

            html = """
                <table class="table table-hover mt-2">
                    <thead>
                        <tr class="table-light">
                            <th>Product</th>
                            <th class="text-end">Sold Qty</th>
                        </tr>
                    </thead>
                    <tbody>
            """

            for product, qty in sales_data:
                html += f"""
                    <tr>
                        <td>
                            <i class='fa fa-cube text-primary me-2'></i>
                            {product.display_name}
                        </td>
                        <td class='text-end fw-bold'>{int(qty)}</td>
                    </tr>
                """

            html += """
                    </tbody>
                </table>
            """

            rec.top_products_html = html

    @api.model
    def get_top_products(self):
        """Top 5 Products logic for Dashboard"""
        return self.env['sale.order.line'].read_group(
            [('order_id.state', 'in', ['sale', 'done'])],
            ['product_id', 'product_uom_qty'],
            ['product_id'],
            limit=5,
            orderby='product_uom_qty desc'
        )

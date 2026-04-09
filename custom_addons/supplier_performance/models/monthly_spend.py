from odoo import models, fields, api


class SupplierMonthlySpend(models.Model):
    _name = 'supplier.monthly.spend'
    _description = 'Supplier Monthly Spend'
    _order = 'month desc'

    partner_id = fields.Many2one('res.partner', string="Supplier", required=True)
    month = fields.Char(string="Month", required=True)
    total_amount = fields.Float(string="Total Spend")

    @api.model
    def compute_monthly_spend(self):
        # Purana data delete (fresh calculation)
        self.search([]).unlink()

        purchase_orders = self.env['purchase.order'].search([
            ('state', 'in', ['purchase', 'done'])
        ])

        data = {}

        for po in purchase_orders:
            if not po.partner_id or not po.date_order:
                continue

            partner_id = po.partner_id.id
            month = po.date_order.strftime('%Y-%m')

            key = (partner_id, month)

            if key not in data:
                data[key] = 0.0

            data[key] += po.amount_total

        # Records create
        for (partner_id, month), amount in data.items():
            self.create({
                'partner_id': partner_id,
                'month': month,
                'total_amount': amount
            })
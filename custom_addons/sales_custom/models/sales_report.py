from odoo import fields, models, tools


class TopCustomerReport(models.Model):
    _name = "top.customer.report"
    _description = "Top Customers Report"
    _auto = False

    partner_id = fields.Many2one("res.partner", string="Customer", readonly=True)
    total_amount = fields.Float(string="Revenue", readonly=True)

    def init(self):
        tools.drop_view_if_exists(self.env.cr, self._table)
        self.env.cr.execute("""
            CREATE OR REPLACE VIEW %s AS (
                SELECT
                    row_number() OVER (
                        ORDER BY SUM(so.amount_total) DESC, rp.commercial_partner_id
                    ) AS id,
                    rp.commercial_partner_id AS partner_id,
                    SUM(so.amount_total) AS total_amount
                FROM sale_order so
                JOIN res_partner rp ON so.partner_id = rp.id
                WHERE so.state IN ('sale', 'done')
                    AND rp.commercial_partner_id IS NOT NULL
                    AND date_trunc('month', so.date_order) = date_trunc('month', CURRENT_DATE)
                GROUP BY rp.commercial_partner_id
                ORDER BY SUM(so.amount_total) DESC, rp.commercial_partner_id
                LIMIT 10
            )
        """ % self._table)

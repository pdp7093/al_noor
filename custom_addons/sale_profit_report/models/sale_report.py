from odoo import fields, models

class SaleReport(models.Model):
    _inherit = 'sale.report'

    cost = fields.Monetary("Cost", readonly=True)
    profit = fields.Monetary("Profit", readonly=True)

    def _select_additional_fields(self):
        res = super()._select_additional_fields()

        # ✅ Odoo 19 में standard_price एक company_dependent Float field है
        # Database में JSONB के रूप में store होता है: {"1": 23.67, "2": 25.00}
        # Format: standard_price->'company_id'::numeric
        
        cost_logic = """
            SUM(l.product_uom_qty * COALESCE(
                (p.standard_price->>CAST(s.company_id AS TEXT))::numeric,
                0
            ))
        """

        res['cost'] = cost_logic

        # ✅ PROFIT: Total Sales - Total Cost
        res['profit'] = f"""
            SUM(l.price_total 
                / {self._case_value_or_one('s.currency_rate')} 
                * {self._case_value_or_one('account_currency_table.rate')}
            ) - ({cost_logic})
        """
        
        return res
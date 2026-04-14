from odoo import http
from odoo.http import request

class NiyuDashboard(http.Controller):
    @http.route('/niyu/dashboard/stats', type='json', auth='user')
    def get_stats(self):
        Forecast = request.env['niyu.forecast.result']
        
        # KPI: Critical Items
        critical_count = Forecast.search_count([('coverage_status', 'in', ['critical', 'out'])])
        
        # KPI: Capital
        # (Odoo search_read is faster than looping)
        lines = Forecast.search_read([('suggested_buy', '>', 0)], ['suggested_buy', 'product_id'])
        # Note: We need cost. search_read doesn't support computed sum easily without logic
        # For speed in Python:
        total_capital = 0
        products = request.env['product.product'].browse([l['product_id'][0] for l in lines])
        product_costs = {p.id: p.standard_price for p in products}
        for l in lines:
            total_capital += l['suggested_buy'] * product_costs.get(l['product_id'][0], 0)

        # Top 5 Urgent
        top_items = Forecast.search_read(
            [('suggested_buy', '>', 0)], 
            ['product_id', 'suggested_buy', 'current_stock', 'days_of_coverage'],
            order='days_of_coverage asc, suggested_buy desc',
            limit=5
        )

        return {
            'critical': critical_count,
            'capital': total_capital,
            'top_items': top_items
        }

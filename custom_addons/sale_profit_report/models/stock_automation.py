from odoo import models, fields, api, _
from odoo.exceptions import UserError
from datetime import datetime, timedelta
import logging

_logger = logging.getLogger(__name__)

class StockReportScheduler(models.Model):
    _name = 'stock.report.scheduler'
    _description = 'Inventory Report Automation'

    name = fields.Char(string="Name", default="Inventory Scheduler Instance")

    # Monthly CEO Report
    def _get_receiver_email(self, role_name, group_xml_id):
        """
        Direct lookup for Administrator and Managers.
        No complex group searching to avoid Odoo 19 field errors.
        """
        # 1. Sabse pehle Administrator ko check karo (Kyuki aapne kaha sab isi par hai)
        if role_name == 'CEO' or role_name == 'Administrator':
            admin = self.env.ref('base.user_admin', raise_if_not_found=False)
            if admin and admin.login:
                return admin.login

        # 2. Baki roles (Store/Factory Manager) ke liye Signature check
        search_query = '%' + role_name + '%'
        user_by_sig = self.env['res.users'].sudo().search([('signature', 'ilike', search_query)], limit=1)
        if user_by_sig and user_by_sig.login:
            return user_by_sig.login
            
        # 3. Hard Fallback: Direct Administrator Email
        return 'dhruvvala67626@gmail.com'
    
    
    
    @api.model
    # calculate monthly revenue and send CEO report via email
    def action_monthly_ceo_report(self):
        """Calculate Real Profit (Sales - Cost) and Sales Report for CEO"""
        target_email = self._get_receiver_email('Administrator', 'base.group_system')
        if not target_email:
            return False

        today = datetime.now()
        month_start = today.replace(day=1, hour=0, minute=0, second=0)
        report_month = today.strftime('%B %Y')

        # 1. Fetch all Posted Invoices for the month
        sale_lines = self.env['account.move.line'].sudo().search([
            ('move_id.move_type', '=', 'out_invoice'),
            ('move_id.state', '=', 'posted'),
            ('move_id.date', '>=', month_start),
            ('product_id', '!=', False),
            ('display_type', 'not in', ['line_section', 'line_note'])
        ])

        total_sales_revenue = 0.0
        total_cost_of_goods = 0.0
        product_stats = {}

        for line in sale_lines:
            # Sales calculation (Price * Qty)
            line_revenue = line.price_subtotal
            total_sales_revenue += line_revenue

            # Cost calculation (Unit Cost * Qty)
            # standard_price product ki cost hoti hai
            line_cost = line.product_id.standard_price * line.quantity
            total_cost_of_goods += line_cost

            # Top products stats
            p_name = line.product_id.name
            product_stats[p_name] = product_stats.get(p_name, 0) + line.quantity

        # 2. Net Profit Calculation (Sales - Cost)
        net_profit = total_sales_revenue - total_cost_of_goods
        profit_color = "#059669" if net_profit >= 0 else "#dc2626"

        # 3. Top 5 Products Sorting
        sorted_products = sorted(product_stats.items(), key=lambda x: x[1], reverse=True)[:5]
        product_rows = ""
        for name, qty in sorted_products:
            product_rows += f"<tr><td style='padding:8px; border:1px solid #ddd;'>{name}</td><td style='padding:8px; border:1px solid #ddd; text-align:center;'>{qty}</td></tr>"

        # 4. Professional HTML Template
        subject = f"Monthly Executive P&L Report - {report_month}"
        body_html = f"""
            <div style="font-family: Arial, sans-serif; max-width: 600px; margin: auto; border: 1px solid #eee; padding: 20px;">
                <h2 style="text-align: center; color: #1f2937; border-bottom: 2px solid #714B67; padding-bottom: 10px;">
                    Al Noor Plastic Industry LLC
                </h2>
                <h3 style="color: #4b5563;">Monthly Profit Analysis ({report_month})</h3>
                
                <table style="width: 100%; border-collapse: collapse; margin-bottom: 20px;">
                    <tr style="background: #f9fafb;">
                        <td style="padding: 12px; border: 1px solid #ddd;"><strong>Total Sales Revenue:</strong></td>
                        <td style="padding: 12px; border: 1px solid #ddd; text-align: right; font-weight: bold; color: #2563eb;"> {total_sales_revenue:,.2f}</td>
                    </tr>
                    <tr>
                        <td style="padding: 12px; border: 1px solid #ddd;"><strong>Total Cost of Goods (COGS):</strong></td>
                        <td style="padding: 12px; border: 1px solid #ddd; text-align: right; font-weight: bold; color: #d97706;"> {total_cost_of_goods:,.2f}</td>
                    </tr>
                    <tr style="background: #f3f4f6; border-top: 2px solid #333;">
                        <td style="padding: 12px; border: 1px solid #ddd;"><strong>Net Profit:</strong></td>
                        <td style="padding: 12px; border: 1px solid #ddd; text-align: right; font-weight: bold; color: {profit_color}; font-size: 18px;"> {net_profit:,.2f}</td>
                    </tr>
                </table>

                <h3 style="color: #4b5563;">Top 5 Selling Products</h3>
                <table style="width: 100%; border-collapse: collapse;">
                    <thead style="background: #714B67; color: white;">
                        <tr><th style="padding:10px; text-align:left;">Product Name</th><th style="padding:10px;">Qty Sold</th></tr>
                    </thead>
                    <tbody>
                        {product_rows if product_rows else "<tr><td colspan='2' style='text-align:center; padding:10px;'>No Sales Data</td></tr>"}
                    </tbody>
                </table>
            </div>
        """
        self._send_mail_logic(subject, body_html, target_email)
        return True

  
    def _send_mail_logic(self, subject, body, email_to):
        if email_to:
            self.env['mail.mail'].sudo().create({
                'subject': subject,
                'body_html': body,
                'email_to': email_to,
            }).send()
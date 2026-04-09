from odoo import models, fields, api, exceptions, _
from odoo.exceptions import UserError
from datetime import datetime, timedelta
import logging

_logger = logging.getLogger(__name__)


class StockPicking(models.Model):
    _inherit = "stock.picking"

    def button_validate(self):
        for move in self.move_ids:
            if move.quantity > move.product_uom_qty:
                if not self.env.user.has_group("stock.group_stock_manager"):
                    raise UserError(
                        _(
                            "Over-receipt detected! Only a Manager can validate quantities greater than ordered."
                        )
                    )
        return super(StockPicking, self).button_validate()


class StockWarehouseOrderpoint(models.Model):
    _inherit = "stock.warehouse.orderpoint"

    def _get_store_manager_emails(self):
        employees = (
            self.env["hr.employee"]
            .sudo()
            .search([("job_id.name", "ilike", "store manager")])
        )
        emails = []
        for emp in employees:
            email = emp.work_email or (emp.user_id.email if emp.user_id else None)
            if email:
                emails.append(email.strip())
        return list(set(emails))

    @api.model
    def cron_low_stock_alert(self):
        _logger.info("Low stock cron started")

        template = self.env.ref(
            "stock_alert.email_template_low_stock_alert", raise_if_not_found=False
        )

        if not template:
            _logger.error("Email template not found")
            return

        low_rules = []

        for rule in self.search([]):
            current_qty = rule.product_id.qty_available
            min_qty = rule.product_min_qty

            if current_qty < min_qty:
                low_rules.append(rule)

        if not low_rules:
            _logger.info("No low stock products")
            return

        manager_emails = self._get_store_manager_emails()
        if not manager_emails:
            _logger.warning("No manager email found")
            return

        try:
            template.with_context(low_products=low_rules).send_mail(
                low_rules[0].id,
                force_send=True,
                email_values={
                    "email_to": ",".join(manager_emails),
                },
            )

            _logger.info("Single consolidated mail sent")

        except Exception:
            _logger.exception("Mail failed")


class StockReportScheduler(models.Model):
    _name = "stock.report.scheduler"
    _description = "Inventory Report Automation"

    @api.model
    def action_send_daily_report(self):
        _logger.info("Daily Stock Report Started")

        # Products fetch
        products = self.env["product.product"].search(
            [("type", "in", ["product", "consu"])], order="name asc"
        )

        if not products:
            _logger.info("No products found")
            return

        # Table build
        table_content = ""
        for prod in products:
            table_content += f"""
                <tr>
                    <td style="border: 1px solid #ddd; padding: 8px;">{prod.display_name}</td>
                    <td style="border: 1px solid #ddd; padding: 8px; text-align: center;">{prod.qty_available}</td>
                    <td style="border: 1px solid #ddd; padding: 8px; text-align: center;">{prod.uom_id.name}</td>
                </tr>
            """

        # Email body
        body_html = f"""
            <div style="font-family: Arial, sans-serif;">
                <h2 style="color: #1a73e8;">Daily Stock Report</h2>

                <p>Hello Store Manager,</p>
                <p>Below is today's inventory summary:</p>

                <table style="width: 100%; border-collapse: collapse;">
                    <thead>
                        <tr style="background-color: #f2f2f2;">
                            <th style="border: 1px solid #ddd; padding: 12px;">Product</th>
                            <th style="border: 1px solid #ddd; padding: 12px;">Quantity</th>
                            <th style="border: 1px solid #ddd; padding: 12px;">UoM</th>
                        </tr>
                    </thead>
                    <tbody>
                        {table_content}
                    </tbody>
                </table>

                <p style="margin-top:20px;">Generated at: {fields.Datetime.now()}</p>
            </div>
        """

        # Get Store Manager Emails (reuse existing logic)
        manager_emails = self.env[
            "stock.warehouse.orderpoint"
        ]._get_store_manager_emails()

        if not manager_emails:
            _logger.warning("No Store Manager email found")
            return

        # Send Email
        self.env["mail.mail"].create(
            {
                "subject": "Daily Inventory Stock Report",
                "body_html": body_html,
                "email_to": ",".join(manager_emails),
            }
        ).send()

        _logger.info("Daily Stock Report Sent Successfully")



class MrpProduction(models.Model):
    _inherit = "mrp.production"

    def button_mark_done(self):
        for line in self.move_raw_ids:
            if line.product_uom_qty > line.forecast_availability:
                raise exceptions.UserError(
                    _(
                        "Action Blocked! '%s' Stock is insufficient. "
                        "First Must complete your stock then after you can complete this production order."
                    )
                    % line.product_id.name
                )
        return super(MrpProduction, self).button_mark_done()


# Note: Inheriting the same model to add more methods
class StockReportSchedulerExtended(models.Model):
    _inherit = "stock.report.scheduler"

    @api.model
    def action_weekly_production_summary(self):
        last_week = datetime.now() - timedelta(days=7)
        orders = self.env["mrp.production"].search(
            [("date_finished", ">=", last_week), ("state", "=", "done")]
        )

        content = "<h2>Weekly Production Summary</h2><table border='1'><tr><th>Product</th><th>Qty Produced</th></tr>"
        for order in orders:
            content += (
                f"<tr><td>{order.product_id.name}</td><td>{order.product_qty}</td></tr>"
            )
        content += "</table>"

        # Factory Manager ka email dhoondhna
        target_email = self.env["stock.report.scheduler"]._get_user_email_by_signature(
            "Factory Manager"
        )

        self.env["mail.mail"].create(
            {
                "subject": "Weekly Factory Report",
                "body_html": content,
                "email_to": target_email,
            }
        ).send()

    @api.model
    def action_monthly_ceo_report(self):
        last_month = datetime.now() - timedelta(days=30)
        sales = self.env["sale.order"].search(
            [("date_order", ">=", last_month), ("state", "in", ["sale", "done"])]
        )
        total_revenue = sum(sales.mapped("amount_total"))

        body = f"<h3>Monthly Business Overview</h3><p><b>Total Revenue: {total_revenue} AED</b></p>"

        # CEO (Administrator) ka email dhoondhna
        target_email = self.env["stock.report.scheduler"]._get_user_email_by_signature(
            "Administrator"
        )

        self.env["mail.mail"].create(
            {
                "subject": "Monthly CEO Performance Report",
                "body_html": body,
                "email_to": target_email,
            }
        ).send()

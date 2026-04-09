from odoo import _, fields, models
from odoo.exceptions import UserError
from odoo.tools.float_utils import float_compare


class SaleOrder(models.Model):
    _inherit = "sale.order"

    def action_confirm(self):
        orders_to_check = self.filtered(lambda o: o.state in ("draft", "sent"))

        for order in orders_to_check:
            order._check_partner_credit_limit()

        return super().action_confirm()

    def _check_partner_credit_limit(self):
        self.ensure_one()

        # ✅ Manager bypass
        if self.env.user.has_group("sales_team.group_sale_manager"):
            return

        partner = self.partner_id.commercial_partner_id.sudo()
        credit_limit = partner.credit_limit or 0.0

        if not credit_limit:
            return

        company = self.company_id

        # ✅ Get current credit safely
        current_credit = partner.with_company(company).sudo().credit or 0.0

        # ✅ Convert order amount
        order_amount = self.currency_id._convert(
            self.amount_total,
            company.currency_id,
            company,
            self.date_order or fields.Date.context_today(self),
        )

        projected_credit = current_credit + order_amount

        # ✅ Check limit
        if float_compare(
            projected_credit,
            credit_limit,
            precision_rounding=company.currency_id.rounding,
        ) <= 0:
            return

        # ✅ Notify manager (activity + notification)
        self._notify_sales_manager(
            partner,
            current_credit,
            projected_credit,
            credit_limit,
            order_amount,
        )

        # ✅ Commit so activity & notification stay
        self.env.cr.commit()

        # ❌ Block confirmation
        raise UserError(
            _(
                "Credit limit exceeded for %(customer)s.\n\n"
                "Credit limit: %(limit).2f\n"
                "Current credit: %(current).2f\n"
                "Order amount: %(amount).2f\n"
                "Projected credit: %(projected).2f"
            )
            % {
                "customer": partner.display_name,
                "limit": credit_limit,
                "current": current_credit,
                "amount": order_amount,
                "projected": projected_credit,
            }
        )

    def _notify_sales_manager(
        self,
        partner,
        current_credit,
        projected_credit,
        credit_limit,
        order_amount,
    ):
        self.ensure_one()

        # ✅ Get managers
        group = self.env.ref("sales_team.group_sale_manager", raise_if_not_found=False)
        if not group:
            return
            
        managers = group.mapped('user_ids').sudo()

        if not managers:
            return

        # ✅ Prepare message
        message_body = _(
            "<b>⚠️ Credit Limit Exceeded</b><br/><br/>"
            "<b>Customer:</b> %(customer)s<br/>"
            "<b>Credit Limit:</b> %(limit).2f<br/>"
            "<b>Current Credit:</b> %(current).2f<br/>"
            "<b>Order Amount:</b> %(amount).2f<br/>"
            "<b>Projected Credit:</b> %(projected).2f"
        ) % {
            "customer": partner.display_name,
            "limit": credit_limit,
            "current": current_credit,
            "amount": order_amount,
            "projected": projected_credit,
        }

        self.message_subscribe(partner_ids=managers.mapped("partner_id").ids)

        self.message_post(
            body=message_body,
            subject=_("Credit Limit Exceeded - %s") % partner.display_name,
            message_type="notification",
            subtype_xmlid="mail.mt_comment",
            partner_ids=managers.mapped("partner_id").ids,
        )

        # ✅ Create activity (task)
       
        activity_type = self.env.ref("mail.mail_activity_data_todo")
        activity_model = self.env["mail.activity"].sudo()
        sale_order_model_id = self.env['ir.model']._get_id('sale.order')
        for manager in managers:

            # Activity (Inbox task)
            activity_model.create({
                "res_model_id": sale_order_model_id,
                "res_id": self.id,
                "user_id": manager.id,
                "activity_type_id": activity_type.id,
                "summary": "Credit Limit Exceeded",
                "note": f"""
        Customer: {partner.display_name}
        Credit Limit: {credit_limit}
        Current Credit: {current_credit}
        Order Amount: {order_amount}
        Projected Credit: {projected_credit}
        """,
                "date_deadline": fields.Date.today(),
            })

            # 🔔 Real notification (THIS is what you want)
            self.env['bus.bus']._sendone(
                manager.partner_id,
                "simple_notification",
                {
                    "title": "Credit Limit Exceeded",
                    "message": f"{partner.display_name} exceeded credit limit",
                    "sticky": True,
                    "warning": True,
                }
            )


















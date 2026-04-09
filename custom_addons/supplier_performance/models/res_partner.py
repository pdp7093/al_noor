from odoo import models, fields, api


class ResPartner(models.Model):
    _inherit = 'res.partner'

    on_time_delivery_rate = fields.Float(
        string="On-Time Delivery (%)",
        compute="_compute_supplier_metrics"
    )

    avg_lead_time = fields.Float(
        string="Average Lead Time (Days)",
        compute="_compute_supplier_metrics"
    )

    return_rate = fields.Float(
        string="Return Rate (%)",
        compute="_compute_supplier_metrics"
    )

    def _compute_supplier_metrics(self):
        for partner in self:
            pickings = self.env['stock.picking'].search([
                ('partner_id', '=', partner.id),
                ('picking_type_code', '=', 'incoming'),
                ('state', '=', 'done')
            ])

            total = len(pickings)
            if not total:
                partner.on_time_delivery_rate = 0
                partner.avg_lead_time = 0
                partner.return_rate = 0
                continue

            # On-time delivery
            on_time = 0
            total_days = 0

            for picking in pickings:
                if picking.scheduled_date and picking.date_done:
                    if picking.date_done <= picking.scheduled_date:
                        on_time += 1

                    days = (picking.date_done - picking.scheduled_date).days
                    total_days += days

            partner.on_time_delivery_rate = (on_time / total) * 100

            # Average lead time
            partner.avg_lead_time = total_days / total if total else 0

            # Return rate (simple logic)
            returns = self.env['stock.picking'].search_count([
                ('partner_id', '=', partner.id),
                ('origin', 'ilike', 'Return'),
            ])

            partner.return_rate = (returns / total) * 100 if total else 0
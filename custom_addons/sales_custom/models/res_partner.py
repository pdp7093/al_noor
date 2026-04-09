from odoo import models, fields
from odoo import api


class ResPartner(models.Model):
    _inherit = 'res.partner'

    credit_limit = fields.Float(string="Credit Limit")

    credit_used = fields.Monetary(
        string="Credit Used",
        compute="_compute_credit_fields",
        currency_field="currency_id",
        store=False,
    )

    remaining_credit = fields.Monetary(
        string="Remaining Credit",
        compute="_compute_credit_fields",
        currency_field="currency_id",
        store=False,
    )

    @api.depends("credit_limit","credit")
    def _compute_credit_fields(self):
        for partner in self:
            # 🔥 use sudo (warna access error aayega)
            credit = partner.sudo().credit or 0.0

            partner.credit_used = credit
            partner.remaining_credit = (partner.credit_limit or 0.0) - credit



            
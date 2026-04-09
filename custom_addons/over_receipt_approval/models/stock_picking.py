from odoo import models, _
from odoo.exceptions import UserError


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    def button_validate(self):
        for picking in self:
            for move in picking.move_ids:

                # Over receipt check
                if move.quantity > move.product_uom_qty:

                    # Allow only Stock Manager
                    if not self.env.user.has_group('stock.group_stock_manager'):
                        raise UserError(_(
                            "Over-receipt detected!\n\n"
                            "You cannot validate more quantity than ordered.\n"
                            "Please contact Inventory Manager for approval."
                        ))

        return super().button_validate()
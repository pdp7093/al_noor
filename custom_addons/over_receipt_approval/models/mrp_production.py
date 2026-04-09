from odoo import models, fields, api, _
from odoo.exceptions import UserError

class MrpProduction(models.Model):
    _inherit = 'mrp.production'

    def action_confirm(self):
        # 1. Check availability for each raw material line
        missing_items = []
        for move in self.move_raw_ids:
            # Check if stock is less than required quantity
            if move.product_id.qty_available < move.product_uom_qty:
                missing_qty = move.product_uom_qty - move.product_id.qty_available
                missing_items.append(
                    _("- %s (Missing: %.2f %s)") % (
                        move.product_id.display_name, 
                        missing_qty, 
                        move.product_uom.name
                    )
                )

        # 2. If missing items found, block the MO and show error
        if missing_items:
            error_msg = _("Cannot confirm Manufacturing Order due to insufficient raw materials:\n\n")
            error_msg += "\n".join(missing_items)
            raise UserError(error_msg)

        # 3. If everything is fine, proceed with standard Odoo confirm logic
        return super(MrpProduction, self).action_confirm()
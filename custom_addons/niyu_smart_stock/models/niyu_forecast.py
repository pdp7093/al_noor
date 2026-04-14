from odoo import api, fields, models, _
from odoo.exceptions import ValidationError
from datetime import timedelta
import ast
import json


class NiyuForecastRun(models.Model):
    _name = 'niyu.forecast.run'
    _description = 'Niyu Forecast Run'
    _order = 'create_date desc, id desc'

    name = fields.Char(required=True, copy=False, default=lambda self: self._default_name())
    job_id = fields.Char(string='Remote Job ID', index=True, copy=False)
    state = fields.Selection([
        ('draft', 'Draft'),
        ('queued', 'Queued'),
        ('running', 'Running'),
        ('done', 'Done'),
        ('failed', 'Failed'),
    ], default='draft', required=True, index=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company, index=True)
    warehouse_ids = fields.Many2many('stock.warehouse', string='Warehouses')
    horizon_days = fields.Integer(default=30, required=True)
    schema_version = fields.Char(default='2.0', required=True)
    started_at = fields.Datetime()
    completed_at = fields.Datetime()
    line_ids = fields.One2many('niyu.forecast.result', 'run_id', string='Lines')
    line_count = fields.Integer(compute='_compute_line_count')
    message = fields.Text()

    @api.model
    def _default_name(self):
        return _('Forecast Run %s') % fields.Datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    @api.depends('line_ids')
    def _compute_line_count(self):
        for rec in self:
            rec.line_count = len(rec.line_ids)

    def action_open_lines(self):
        self.ensure_one()
        return {
            'name': _('Forecast Lines'),
            'type': 'ir.actions.act_window',
            'res_model': 'niyu.forecast.result',
            'view_mode': 'list,form',
            'domain': [('run_id', '=', self.id)],
            'context': {'default_run_id': self.id},
            'target': 'current',
        }


class NiyuForecastExclusion(models.Model):
    _name = 'niyu.forecast.exclusion'
    _description = 'Niyu Forecast Exclusion'
    _order = 'active desc, id desc'

    name = fields.Char(required=True, help="Short rule name. Example: Obsolete SKUs or Project-only items.")
    active = fields.Boolean(default=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company, index=True)

    product_ids = fields.Many2many(
        'product.product',
        'niyu_exclusion_product_rel',
        'exclusion_id',
        'product_id',
        string='Products',
    )
    product_tmpl_ids = fields.Many2many(
        'product.template',
        'niyu_exclusion_template_rel',
        'exclusion_id',
        'product_tmpl_id',
        string='Product Templates',
    )
    categ_ids = fields.Many2many(
        'product.category',
        'niyu_exclusion_category_rel',
        'exclusion_id',
        'category_id',
        string='Categories',
    )

    scope_summary = fields.Char(compute='_compute_scope_summary')
    match_count = fields.Integer(string='Matching Products', compute='_compute_match_count')
    reason = fields.Text(help="Optional note for planners. Example: obsolete, internal-use, sample-only, or non-managed items.")

    @api.constrains('product_ids', 'product_tmpl_ids', 'categ_ids')
    def _check_any_scope(self):
        for rec in self:
            if not rec.product_ids and not rec.product_tmpl_ids and not rec.categ_ids:
                raise ValidationError(_("Add at least one product, template, or category to exclude."))

    @api.depends('product_ids', 'product_tmpl_ids', 'categ_ids')
    def _compute_scope_summary(self):
        for rec in self:
            parts = []
            if rec.product_ids:
                parts.append(_('%s products') % len(rec.product_ids))
            if rec.product_tmpl_ids:
                parts.append(_('%s templates') % len(rec.product_tmpl_ids))
            if rec.categ_ids:
                parts.append(_('%s categories') % len(rec.categ_ids))
            rec.scope_summary = ', '.join(parts) if parts else _('No scope selected')

    @api.depends('product_ids', 'product_tmpl_ids', 'categ_ids')
    def _compute_match_count(self):
        for rec in self:
            rec.match_count = len(rec._get_matching_products())

    def _get_matching_products(self):
        self.ensure_one()
        Product = self.env['product.product']
        products = Product.browse()

        if self.product_ids:
            products |= self.product_ids

        if self.product_tmpl_ids:
            products |= self.product_tmpl_ids.mapped('product_variant_ids')

        if self.categ_ids:
            products |= Product.search([('categ_id', 'child_of', self.categ_ids.ids)])

        return products

    @api.model
    def get_excluded_product_ids(self, company=False):
        company = company or self.env.company
        rules = self.search([
            ('active', '=', True),
            ('company_id', '=', company.id),
        ])
        product_ids = set()
        for rule in rules:
            product_ids.update(rule._get_matching_products().ids)
        return product_ids

    def action_open_matching_products(self):
        self.ensure_one()
        return {
            'name': _('Excluded Products'),
            'type': 'ir.actions.act_window',
            'res_model': 'product.product',
            'view_mode': 'list,form',
            'domain': [('id', 'in', self._get_matching_products().ids)],
            'target': 'current',
        }

    def action_open_matching_lines(self):
        self.ensure_one()
        return {
            'name': _('Affected Action Lines'),
            'type': 'ir.actions.act_window',
            'res_model': 'niyu.forecast.result',
            'view_mode': 'list,form',
            'domain': [
                ('company_id', '=', self.company_id.id),
                ('product_id', 'in', self._get_matching_products().ids),
            ],
            'target': 'current',
        }


class NiyuReplenishmentPolicy(models.Model):
    _name = 'niyu.replenishment.policy'
    _description = 'Niyu Replenishment Policy'
    _order = 'priority desc, id desc'

    name = fields.Char(required=True, help="Short rule name. Example: Imported fast movers or Vendor X carton rules.")
    active = fields.Boolean(default=True)
    priority = fields.Integer(default=10, help="Higher priority wins when multiple rules match the same product.")
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company, index=True)

    warehouse_id = fields.Many2one(
        'stock.warehouse',
        index=True,
        help="Optional. Leave empty to apply to all warehouses.",
    )
    vendor_id = fields.Many2one(
        'res.partner',
        string='Vendor',
        domain=[('supplier_rank', '>', 0)],
        index=True,
        help="Optional. Use when this rule only applies to a specific supplier.",
    )

    product_id = fields.Many2one(
        'product.product',
        index=True,
        help="Most specific scope. Use for one exact product.",
    )
    product_tmpl_id = fields.Many2one(
        'product.template',
        string='Product Template',
        index=True,
        help="Use when the same rule should apply to all variants of one template.",
    )
    categ_id = fields.Many2one(
        'product.category',
        string='Product Category',
        index=True,
        help="Broadest scope. Applies to the whole category tree.",
    )

    scope_summary = fields.Char(compute='_compute_summaries')
    behavior_summary = fields.Char(compute='_compute_summaries')

    service_level_target = fields.Float(
        default=95.0,
        help="Target service level in percent. Higher means lower stockout risk but more inventory.",
    )
    min_days_of_stock = fields.Float(
        default=7.0,
        help="Below this coverage, the item is considered at risk.",
    )
    max_days_of_stock = fields.Float(
        default=60.0,
        help="Above this coverage, the item is considered overstocked.",
    )
    lead_time_days = fields.Float(
        default=0.0,
        help="Override lead time in days. Leave zero to use vendor lead time when available.",
    )
    safety_stock_days = fields.Float(
        default=0.0,
        help="Extra buffer expressed in days of demand.",
    )

    fixed_order_qty = fields.Float(
        default=0.0,
        help="If set, suggested orders round up to this fixed step size.",
    )
    qty_multiple = fields.Float(
        default=1.0,
        help="If set above 1, suggested orders round up to this multiple.",
    )
    min_order_qty = fields.Float(
        string='MOQ',
        default=0.0,
        help="Minimum order quantity allowed for matching items.",
    )

    include_incoming_po = fields.Boolean(
        default=True,
        help="Count incoming confirmed purchase orders before suggesting more buying.",
    )
    include_incoming_transfers = fields.Boolean(
        default=True,
        help="Count incoming internal transfers before suggesting more buying.",
    )
    include_draft_rfqs = fields.Boolean(
        default=False,
        help="Include draft RFQs in incoming supply calculations.",
    )

    note = fields.Text(help="Optional business context shown to planners and implementers.")

    @api.constrains('product_id', 'product_tmpl_id', 'categ_id')
    def _check_scope_specificity(self):
        for rec in self:
            scope_count = sum(bool(x) for x in [rec.product_id, rec.product_tmpl_id, rec.categ_id])
            if scope_count > 1:
                raise ValidationError(_(
                    "Use only one product scope per rule: either Product, Product Template, or Category."
                ))

    @api.depends(
        'warehouse_id', 'vendor_id', 'product_id', 'product_tmpl_id', 'categ_id',
        'service_level_target', 'min_days_of_stock', 'max_days_of_stock',
        'lead_time_days', 'safety_stock_days', 'fixed_order_qty', 'qty_multiple', 'min_order_qty'
    )
    def _compute_summaries(self):
        for rec in self:
            scope_parts = []

            if rec.product_id:
                scope_parts.append(_('product: %s') % rec.product_id.display_name)
            elif rec.product_tmpl_id:
                scope_parts.append(_('template: %s') % rec.product_tmpl_id.display_name)
            elif rec.categ_id:
                scope_parts.append(_('category: %s') % rec.categ_id.display_name)
            else:
                scope_parts.append(_('all products'))

            if rec.vendor_id:
                scope_parts.append(_('vendor: %s') % rec.vendor_id.display_name)
            if rec.warehouse_id:
                scope_parts.append(_('warehouse: %s') % rec.warehouse_id.display_name)

            rec.scope_summary = ' • '.join(scope_parts)

            behavior_parts = [
                _('service %s%%') % (rec.service_level_target or 0.0),
                _('DOS %s-%s') % (rec.min_days_of_stock or 0.0, rec.max_days_of_stock or 0.0),
            ]

            if rec.lead_time_days:
                behavior_parts.append(_('LT %sd') % rec.lead_time_days)
            if rec.safety_stock_days:
                behavior_parts.append(_('buffer %sd') % rec.safety_stock_days)
            if rec.min_order_qty:
                behavior_parts.append(_('MOQ %s') % rec.min_order_qty)
            if rec.fixed_order_qty:
                behavior_parts.append(_('fixed %s') % rec.fixed_order_qty)
            elif rec.qty_multiple and rec.qty_multiple > 1:
                behavior_parts.append(_('multiple %s') % rec.qty_multiple)

            rec.behavior_summary = ' • '.join(behavior_parts)

    def action_open_matching_lines(self):
        self.ensure_one()
        domain = [('company_id', '=', self.company_id.id)]
        if self.warehouse_id:
            domain.append(('warehouse_id', '=', self.warehouse_id.id))
        if self.product_id:
            domain.append(('product_id', '=', self.product_id.id))
        elif self.product_tmpl_id:
            domain.append(('product_tmpl_id', '=', self.product_tmpl_id.id))
        elif self.categ_id:
            domain.append(('product_id.categ_id', 'child_of', self.categ_id.id))
        if self.vendor_id:
            domain.append(('vendor_id', '=', self.vendor_id.id))
        return {
            'name': _('Matching Forecast Lines'),
            'type': 'ir.actions.act_window',
            'res_model': 'niyu.forecast.result',
            'view_mode': 'list,form',
            'domain': domain,
            'target': 'current',
        }


class NiyuForecastTransferPlan(models.Model):
    _name = 'niyu.forecast.transfer.plan'
    _description = 'Niyu Forecast Transfer Allocation'
    _order = 'sequence asc, qty desc, id asc'

    sequence = fields.Integer(default=10)
    result_id = fields.Many2one('niyu.forecast.result', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='result_id.company_id', store=True, readonly=True)
    product_id = fields.Many2one(related='result_id.product_id', store=True, readonly=True)
    dest_warehouse_id = fields.Many2one(related='result_id.warehouse_id', store=True, readonly=True)
    source_warehouse_id = fields.Many2one('stock.warehouse', required=True, index=True)

    qty = fields.Float(required=True, default=0.0)
    coverage_pct = fields.Float(default=0.0)
    source_available_qty = fields.Float(default=0.0)
    source_protected_qty = fields.Float(default=0.0)
    source_remaining_after = fields.Float(default=0.0)

    line_summary = fields.Char(compute='_compute_line_summary')

    @api.depends('source_warehouse_id', 'qty', 'coverage_pct')
    def _compute_line_summary(self):
        for rec in self:
            if rec.source_warehouse_id:
                rec.line_summary = _('%s → %s (%.2f%%)') % (
                    rec.source_warehouse_id.display_name,
                    rec.qty or 0.0,
                    rec.coverage_pct or 0.0,
                )
            else:
                rec.line_summary = _('No source selected')


class NiyuForecastResult(models.Model):
    _name = 'niyu.forecast.result'
    _description = 'Niyu Forecast / Replenishment Line'
    _rec_name = 'product_id'
    _order = 'coverage_status asc, suggested_buy desc, suggested_transfer_qty desc, id desc'

    run_id = fields.Many2one('niyu.forecast.run', string='Run', index=True, ondelete='set null')
    policy_id = fields.Many2one('niyu.replenishment.policy', string='Planning Rule', index=True, ondelete='set null')
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company, index=True)
    warehouse_id = fields.Many2one('stock.warehouse', index=True)

    product_id = fields.Many2one('product.product', string='Product', required=True, index=True)
    product_tmpl_id = fields.Many2one(
        'product.template',
        related='product_id.product_tmpl_id',
        string='Product Template',
        store=True,
        readonly=True,
    )
    vendor_id = fields.Many2one('res.partner', string='Preferred Vendor', domain=[('supplier_rank', '>', 0)], index=True)

    current_stock = fields.Float(string='Current Qty', default=0.0)
    reserved_qty = fields.Float(string='Reserved Qty', default=0.0)
    incoming_po_qty = fields.Float(string='Incoming PO Qty', default=0.0)
    incoming_transfer_qty = fields.Float(string='Incoming Transfer Qty', default=0.0)
    net_available = fields.Float(string='Net Available', compute='_compute_net_available', store=True)

    forecast_30d = fields.Float(string='Forecast 30d')
    forecast_60d = fields.Float(string='Forecast 60d')
    forecast_90d = fields.Float(string='Forecast 90d')
    forecast_120d = fields.Float(string='Forecast 120d')
    chart_data = fields.Text(string='Chart JSON')
    confidence_score = fields.Float(string='Confidence Score')

    lead_time_days = fields.Float(string='Lead Time (Days)', default=0.0)
    service_level_target = fields.Float(string='Service Level %', default=95.0)
    safety_stock = fields.Float(string='Safety Stock', default=0.0)
    reorder_point = fields.Float(string='Reorder Point', default=0.0)
    target_stock = fields.Float(string='Target Stock', default=0.0)
    recommended_qty = fields.Float(string='Recommended Qty', default=0.0)
    rounded_order_qty = fields.Float(string='Rounded Order Qty', default=0.0)

    suggested_transfer_qty = fields.Float(string='Suggested Transfer Qty', default=0.0)
    transfer_source_warehouse_id = fields.Many2one('stock.warehouse', string='Primary Transfer Source', index=True)
    transfer_coverage_pct = fields.Float(string='Transfer Coverage %', default=0.0)
    transfer_plan_ids = fields.One2many('niyu.forecast.transfer.plan', 'result_id', string='Transfer Plans')
    donor_count = fields.Integer(string='Donor Warehouses', compute='_compute_transfer_plan_summary', store=True)
    transfer_sources_summary = fields.Char(string='Transfer Sources', compute='_compute_transfer_plan_summary', store=True)

    daily_sales = fields.Float(string='Avg Daily Demand', compute='_compute_coverage', store=True)
    days_of_coverage = fields.Float(string='Days of Coverage', compute='_compute_coverage', store=True)
    coverage_status = fields.Selection([
        ('critical', 'Critical'),
        ('warning', 'Overstock'),
        ('normal', 'Healthy'),
        ('out', 'Stockout'),
        ('ignored', 'Ignored'),
    ], string='Status', compute='_compute_coverage', store=True)

    action_type = fields.Selection([
        ('buy', 'Buy'),
        ('transfer', 'Transfer'),
        ('split', 'Split'),
        ('watch', 'Watch'),
        ('ignore', 'Ignore'),
    ], string='Action', default='watch')
    action_reason = fields.Text(string='Action Reason')

    exception_bucket = fields.Selection([
        ('buy_now', 'Buy Now'),
        ('transfer_now', 'Transfer Now'),
        ('split_now', 'Transfer + Buy'),
        ('setup', 'Setup Needed'),
        ('watch', 'Watch'),
        ('ignored', 'Ignored'),
    ], string='Action Required', compute='_compute_planner_flags', store=True)

    setup_issue = fields.Selection([
        ('missing_vendor', 'Missing Vendor'),
        ('missing_transfer_source', 'Missing Transfer Source'),
    ], string='Setup Needed', compute='_compute_planner_flags', store=True)

    has_policy = fields.Boolean(string='Has Planning Rule', compute='_compute_planner_flags', store=True)
    can_create_po = fields.Boolean(string='Can Create RFQ', compute='_compute_planner_flags', store=True)
    can_create_transfer = fields.Boolean(string='Can Create Transfer', compute='_compute_planner_flags', store=True)
    action_guidance = fields.Text(string='Recommended Steps', compute='_compute_planner_flags', store=True)
    needs_attention = fields.Boolean(string='Needs Attention', compute='_compute_planner_flags', store=True)

    purchase_order_ids = fields.Many2many(
        'purchase.order',
        'niyu_forecast_result_purchase_order_rel',
        'result_id',
        'purchase_order_id',
        string='Linked RFQs / Purchase Orders',
        copy=False,
    )
    transfer_picking_ids = fields.Many2many(
        'stock.picking',
        'niyu_forecast_result_transfer_picking_rel',
        'result_id',
        'picking_id',
        string='Linked Internal Transfers',
        copy=False,
    )
    purchase_order_count = fields.Integer(string='RFQs / POs', compute='_compute_execution_tracking', store=True)
    transfer_picking_count = fields.Integer(string='Transfers', compute='_compute_execution_tracking', store=True)
    purchase_order_refs = fields.Char(string='RFQ / PO References', compute='_compute_execution_tracking', store=True)
    transfer_picking_refs = fields.Char(string='Transfer References', compute='_compute_execution_tracking', store=True)
    po_last_action_at = fields.Datetime(string='RFQ Created On', copy=False)
    transfer_last_action_at = fields.Datetime(string='Transfer Created On', copy=False)

    buy_step_state = fields.Selection([
        ('na', 'Not Needed'),
        ('pending', 'Pending'),
        ('blocked', 'Blocked'),
        ('created', 'Created'),
    ], string='Buy Step', compute='_compute_execution_tracking', store=True)

    transfer_step_state = fields.Selection([
        ('na', 'Not Needed'),
        ('pending', 'Pending'),
        ('blocked', 'Blocked'),
        ('created', 'Created'),
    ], string='Transfer Step', compute='_compute_execution_tracking', store=True)

    execution_status = fields.Selection([
        ('not_needed', 'No Action Needed'),
        ('pending', 'Pending'),
        ('partial', 'Partially Executed'),
        ('done', 'Executed'),
        ('blocked', 'Blocked'),
        ('ignored', 'Ignored'),
    ], string='Execution Status', compute='_compute_execution_tracking', store=True)

    execution_guidance = fields.Text(string='Execution Guidance', compute='_compute_execution_tracking', store=True)

    abc_class = fields.Selection([('a', 'A'), ('b', 'B'), ('c', 'C')], string='ABC Class')
    xyz_class = fields.Selection([('x', 'X'), ('y', 'Y'), ('z', 'Z')], string='XYZ Class')

    ignored = fields.Boolean(string='Ignored', default=False)
    ignore_source = fields.Selection([
        ('manual', 'Manual'),
        ('exclusion', 'Exclusion Rule'),
    ], string='Ignore Source')

    has_vendor = fields.Boolean(string='Has Vendor', compute='_compute_has_vendor', store=True)
    last_updated = fields.Datetime(string='Last Sync')
    cost_to_restock = fields.Float(string='Capital Needed', compute='_compute_cost', store=True)
    suggested_buy = fields.Float(string='Suggested Buy', compute='_compute_buy', store=True)

    _sql_constraints = [
        (
            'product_warehouse_company_uniq',
            'unique(product_id, warehouse_id, company_id)',
            'Only one replenishment line per product, warehouse, and company is allowed.'
        ),
    ]

    @api.depends('current_stock', 'reserved_qty', 'incoming_po_qty', 'incoming_transfer_qty')
    def _compute_net_available(self):
        for rec in self:
            rec.net_available = (rec.current_stock or 0.0) - (rec.reserved_qty or 0.0) + (rec.incoming_po_qty or 0.0) + (rec.incoming_transfer_qty or 0.0)

    @api.depends('product_id.seller_ids', 'vendor_id')
    def _compute_has_vendor(self):
        for rec in self:
            rec.has_vendor = bool(rec.vendor_id or rec.product_id.seller_ids)

    @api.depends('transfer_plan_ids.qty', 'transfer_plan_ids.source_warehouse_id')
    def _compute_transfer_plan_summary(self):
        for rec in self:
            plans = rec.transfer_plan_ids.sorted(key=lambda p: (-p.qty, p.source_warehouse_id.display_name if p.source_warehouse_id else ''))
            rec.donor_count = len(plans)
            if plans:
                rec.transfer_sources_summary = ' + '.join(
                    ['%s %.2f' % (p.source_warehouse_id.display_name, p.qty) for p in plans]
                )
            else:
                rec.transfer_sources_summary = False

    @api.depends(
        'policy_id', 'ignored', 'action_type', 'has_vendor',
        'transfer_plan_ids.qty', 'transfer_plan_ids.source_warehouse_id',
        'suggested_buy', 'suggested_transfer_qty', 'donor_count'
    )
    def _compute_planner_flags(self):
        for rec in self:
            rec.has_policy = bool(rec.policy_id)

            setup_issue = False
            has_transfer_plan = bool(rec.transfer_plan_ids)

            can_create_po = bool(
                not rec.ignored
                and rec.action_type in ('buy', 'split')
                and (rec.suggested_buy or 0.0) > 0
                and rec.has_vendor
            )
            can_create_transfer = bool(
                not rec.ignored
                and rec.action_type in ('transfer', 'split')
                and (rec.suggested_transfer_qty or 0.0) > 0
                and has_transfer_plan
            )

            if not rec.ignored:
                if rec.action_type in ('buy', 'split') and (rec.suggested_buy or 0.0) > 0 and not rec.has_vendor:
                    setup_issue = 'missing_vendor'
                elif rec.action_type in ('transfer', 'split') and (rec.suggested_transfer_qty or 0.0) > 0 and not has_transfer_plan:
                    setup_issue = 'missing_transfer_source'

            rec.setup_issue = setup_issue
            rec.can_create_po = can_create_po
            rec.can_create_transfer = can_create_transfer

            if rec.ignored:
                rec.exception_bucket = 'ignored'
            elif setup_issue:
                rec.exception_bucket = 'setup'
            elif rec.action_type == 'split' and ((rec.suggested_transfer_qty or 0.0) > 0 or (rec.suggested_buy or 0.0) > 0):
                rec.exception_bucket = 'split_now'
            elif rec.action_type == 'transfer' and (rec.suggested_transfer_qty or 0.0) > 0:
                rec.exception_bucket = 'transfer_now'
            elif rec.action_type == 'buy' and (rec.suggested_buy or 0.0) > 0:
                rec.exception_bucket = 'buy_now'
            else:
                rec.exception_bucket = 'watch'

            rec.needs_attention = rec.exception_bucket in ('buy_now', 'transfer_now', 'split_now', 'setup')

            if rec.ignored:
                rec.action_guidance = _('Ignored line. No action required.')
            elif setup_issue == 'missing_vendor':
                rec.action_guidance = _(
                    'Setup needed: this line still needs a vendor before an RFQ can be created. '
                    'Add a vendor on the product or set a vendor through a planning rule.'
                )
            elif setup_issue == 'missing_transfer_source':
                rec.action_guidance = _(
                    'Setup needed: this line expects a transfer, but no valid donor allocation is available. '
                    'Review stock in other warehouses and run sync again.'
                )
            elif rec.action_type == 'split':
                if rec.donor_count > 1:
                    rec.action_guidance = _(
                        'This line needs both actions. Step 1: create transfers from the planned donor warehouses. '
                        'Step 2: create the RFQ for the remaining buy quantity.'
                    )
                else:
                    rec.action_guidance = _(
                        'This line needs both actions. Step 1: create the internal transfer. '
                        'Step 2: create the RFQ for the remaining buy quantity.'
                    )
            elif rec.action_type == 'transfer':
                if rec.donor_count > 1:
                    rec.action_guidance = _(
                        'This line should be replenished by transfers from multiple source warehouses. '
                        'Create the planned internal transfers.'
                    )
                else:
                    rec.action_guidance = _(
                        'This line should be replenished by internal transfer. Create the transfer from the suggested source warehouse.'
                    )
            elif rec.action_type == 'buy':
                rec.action_guidance = _(
                    'This line should be replenished by purchase. Create or update the RFQ for the suggested quantity.'
                )
            else:
                rec.action_guidance = _(
                    'No immediate operational action is required. Keep this line under observation.'
                )

    @api.depends(
        'ignored', 'action_type', 'setup_issue',
        'suggested_buy', 'suggested_transfer_qty',
        'can_create_po', 'can_create_transfer',
        'purchase_order_ids', 'transfer_picking_ids'
    )
    def _compute_execution_tracking(self):
        for rec in self:
            po_needed = bool(
                not rec.ignored
                and rec.action_type in ('buy', 'split')
                and (rec.suggested_buy or 0.0) > 0
            )
            transfer_needed = bool(
                not rec.ignored
                and rec.action_type in ('transfer', 'split')
                and (rec.suggested_transfer_qty or 0.0) > 0
            )

            po_names = sorted(rec.purchase_order_ids.mapped('name'))
            transfer_names = sorted(rec.transfer_picking_ids.mapped('name'))

            rec.purchase_order_count = len(rec.purchase_order_ids)
            rec.transfer_picking_count = len(rec.transfer_picking_ids)
            rec.purchase_order_refs = ', '.join(po_names) if po_names else False
            rec.transfer_picking_refs = ', '.join(transfer_names) if transfer_names else False

            if not po_needed:
                rec.buy_step_state = 'na'
            elif rec.purchase_order_ids:
                rec.buy_step_state = 'created'
            elif rec.can_create_po:
                rec.buy_step_state = 'pending'
            else:
                rec.buy_step_state = 'blocked'

            if not transfer_needed:
                rec.transfer_step_state = 'na'
            elif rec.transfer_picking_ids:
                rec.transfer_step_state = 'created'
            elif rec.can_create_transfer:
                rec.transfer_step_state = 'pending'
            else:
                rec.transfer_step_state = 'blocked'

            if rec.ignored:
                rec.execution_status = 'ignored'
                rec.execution_guidance = _('Ignored line. No execution required.')
                continue

            if rec.setup_issue:
                rec.execution_status = 'blocked'
                if rec.setup_issue == 'missing_vendor':
                    rec.execution_guidance = _(
                        'Execution is blocked. Add a vendor first, then create the RFQ.'
                    )
                elif rec.setup_issue == 'missing_transfer_source':
                    rec.execution_guidance = _(
                        'Execution is blocked. No valid donor transfer plan is available yet.'
                    )
                else:
                    rec.execution_guidance = _('Execution is blocked until setup is completed.')
                continue

            needed_steps = int(po_needed) + int(transfer_needed)
            done_steps = int(po_needed and bool(rec.purchase_order_ids)) + int(transfer_needed and bool(rec.transfer_picking_ids))

            if needed_steps == 0:
                rec.execution_status = 'not_needed'
                rec.execution_guidance = _('No execution is required for this line right now.')
            elif done_steps == 0:
                rec.execution_status = 'pending'
                if rec.action_type == 'split':
                    rec.execution_guidance = _(
                        'Nothing has been created yet. Create the transfer first, then create the RFQ for the remaining buy quantity.'
                    )
                elif rec.action_type == 'transfer':
                    rec.execution_guidance = _(
                        'No transfer document is linked yet. Create the planned internal transfer.'
                    )
                else:
                    rec.execution_guidance = _(
                        'No RFQ is linked yet. Create or update the RFQ for this line.'
                    )
            elif done_steps < needed_steps:
                rec.execution_status = 'partial'
                if rec.action_type == 'split':
                    if rec.transfer_picking_ids and not rec.purchase_order_ids:
                        rec.execution_guidance = _(
                            'Transfers are already linked. Next step: create the RFQ for the remaining buy quantity.'
                        )
                    elif rec.purchase_order_ids and not rec.transfer_picking_ids:
                        rec.execution_guidance = _(
                            'RFQ is already linked. Next step: create the planned internal transfers.'
                        )
                    else:
                        rec.execution_guidance = _(
                            'This line is partially executed. Review linked documents and complete the remaining step.'
                        )
                else:
                    rec.execution_guidance = _(
                        'This line is partially executed. Review linked documents and complete the remaining step.'
                    )
            else:
                rec.execution_status = 'done'
                if rec.action_type == 'split':
                    rec.execution_guidance = _(
                        'Both execution steps are already linked to documents. Open the transfer(s) or RFQ to review.'
                    )
                elif rec.action_type == 'transfer':
                    rec.execution_guidance = _(
                        'Internal transfer is already linked. Open the transfer document to review or process it.'
                    )
                else:
                    rec.execution_guidance = _(
                        'RFQ / PO is already linked. Open the document to review or continue purchasing.'
                    )

    @api.depends('forecast_30d', 'net_available', 'recommended_qty', 'rounded_order_qty', 'target_stock', 'ignored', 'action_type')
    def _compute_buy(self):
        for rec in self:
            if rec.ignored or rec.action_type == 'transfer':
                rec.suggested_buy = 0.0
                continue
            if rec.rounded_order_qty > 0:
                needed = rec.rounded_order_qty
            elif rec.recommended_qty > 0:
                needed = rec.recommended_qty
            elif rec.target_stock > 0:
                needed = rec.target_stock - rec.net_available
            else:
                needed = rec.forecast_30d - rec.net_available
            rec.suggested_buy = max(0.0, needed)

    @api.depends('forecast_30d', 'net_available', 'ignored', 'policy_id.min_days_of_stock', 'policy_id.max_days_of_stock')
    def _compute_coverage(self):
        for rec in self:
            daily = rec.forecast_30d / 30.0 if rec.forecast_30d else 0.0
            rec.daily_sales = daily
            available_for_coverage = rec.net_available or 0.0

            if daily > 0:
                rec.days_of_coverage = available_for_coverage / daily
            elif available_for_coverage <= 0 and daily == 0:
                rec.days_of_coverage = 0.0
            else:
                rec.days_of_coverage = 999.0

            min_days = rec.policy_id.min_days_of_stock or 7.0
            max_days = rec.policy_id.max_days_of_stock or 60.0
            if rec.ignored:
                rec.coverage_status = 'ignored'
            elif available_for_coverage <= 0 and rec.forecast_30d > 0:
                rec.coverage_status = 'out'
            elif rec.days_of_coverage < min_days:
                rec.coverage_status = 'critical'
            elif rec.days_of_coverage > max_days:
                rec.coverage_status = 'warning'
            else:
                rec.coverage_status = 'normal'

    @api.depends('suggested_buy', 'product_id.standard_price')
    def _compute_cost(self):
        for rec in self:
            rec.cost_to_restock = (rec.suggested_buy or 0.0) * (rec.product_id.standard_price or 0.0)

    @api.model
    def _safe_parse_chart(self, raw_chart):
        if not raw_chart:
            return []
        try:
            loaded = json.loads(raw_chart)
            return loaded.get('v', []) if isinstance(loaded, dict) else []
        except Exception:
            try:
                loaded = ast.literal_eval(raw_chart)
                return loaded.get('v', []) if isinstance(loaded, dict) else []
            except Exception:
                return []

    def _get_preferred_vendor(self):
        self.ensure_one()
        if self.vendor_id:
            return self.vendor_id
        seller = self.product_id.seller_ids[:1]
        return seller.partner_id if seller else False

    def _get_supplierinfo_for_vendor(self, vendor):
        self.ensure_one()
        if not vendor:
            return self.env['product.supplierinfo']
        sellers = self.product_id.seller_ids.filtered(lambda s: s.partner_id == vendor)
        return sellers[:1]

    def _get_purchase_uom(self):
        self.ensure_one()
        product = self.product_id
        template = product.product_tmpl_id
        return (
            getattr(product, 'uom_po_id', False)
            or getattr(template, 'uom_po_id', False)
            or getattr(product, 'uom_id', False)
            or getattr(template, 'uom_id', False)
        )

    def _get_purchase_line_uom_field_name(self):
        PurchaseOrderLine = self.env['purchase.order.line']
        if 'product_uom' in PurchaseOrderLine._fields:
            return 'product_uom'
        if 'product_uom_id' in PurchaseOrderLine._fields:
            return 'product_uom_id'
        return False

    def _get_stock_move_uom_field_name(self):
        StockMove = self.env['stock.move']
        if 'product_uom' in StockMove._fields:
            return 'product_uom'
        if 'product_uom_id' in StockMove._fields:
            return 'product_uom_id'
        return False

    def _get_stock_move_text_field_name(self):
        StockMove = self.env['stock.move']
        for field_name in ['description_picking', 'reference', 'origin']:
            if field_name in StockMove._fields:
                return field_name
        return False

    def _get_internal_picking_type(self, source_warehouse):
        self.ensure_one()
        if not source_warehouse:
            return self.env['stock.picking.type']
        picking_type = source_warehouse.int_type_id
        if picking_type:
            return picking_type
        return self.env['stock.picking.type'].search([
            ('code', '=', 'internal'),
            ('warehouse_id', '=', source_warehouse.id),
        ], limit=1)

    @api.model
    def get_dashboard_stats(self):
        actionable_domain = [('ignored', '=', False)]
        ICP = self.env['ir.config_parameter'].sudo()
        plan_tier = ICP.get_param('niyu.last_tier', '')
        plan_sku_limit = int(ICP.get_param('niyu.last_sku_limit', '0') or 0)
        plan_manual_limit = int(ICP.get_param('niyu.last_manual_limit', '0') or 0)
        plan_scheduled_limit = int(ICP.get_param('niyu.last_scheduled_limit', '0') or 0)
        plan_max_horizon_days = int(ICP.get_param('niyu.last_max_horizon_days', '0') or 0)
        plan_model_type = ICP.get_param('niyu.last_model_type', '')
        plan_seen_at = ICP.get_param('niyu.last_backend_seen_at', '')
        last_sync_str = ICP.get_param('niyu.last_sync_time', 'Never')
        sync_status = ICP.get_param('niyu.last_sync_status', 'unknown')
        sync_msg = ICP.get_param('niyu.last_sync_msg', '')

        latest_run = self.env['niyu.forecast.run'].search([], order='id desc', limit=1)
        latest_run_state_label = ''
        if latest_run:
            latest_run_state_label = dict(latest_run._fields['state'].selection).get(latest_run.state, latest_run.state)

        currency = self.env.company.currency_id
        actionable_lines = self.search(actionable_domain)
        attention_lines = actionable_lines.filtered(lambda r: r.needs_attention)
        buy_lines = actionable_lines.filtered(lambda r: r.action_type in ('buy', 'split') and (r.suggested_buy or 0.0) > 0)
        transfer_lines = actionable_lines.filtered(lambda r: r.action_type in ('transfer', 'split') and (r.suggested_transfer_qty or 0.0) > 0)

        def _count(extra_domain):
            return self.search_count(actionable_domain + extra_domain)

        def _estimate_unit_cost(rec):
            unit_cost = float(rec.product_id.standard_price or 0.0)
            if unit_cost > 0:
                return unit_cost

            vendor = rec.vendor_id or rec._get_preferred_vendor()
            seller = rec._get_supplierinfo_for_vendor(vendor) if vendor else False
            seller_price = float(seller.price or 0.0) if seller else 0.0
            return seller_price if seller_price > 0 else 0.0

        def _estimate_buy_budget(records):
            total = 0.0
            for rec in records:
                total += (rec.suggested_buy or 0.0) * _estimate_unit_cost(rec)
            return round(total, 2)

        queue_counts = {
            'buy_now': _count([('exception_bucket', '=', 'buy_now')]),
            'transfer_now': _count([('exception_bucket', '=', 'transfer_now')]),
            'split_now': _count([('exception_bucket', '=', 'split_now')]),
            'setup': _count([('exception_bucket', '=', 'setup')]),
            'watch': _count([('exception_bucket', '=', 'watch')]),
            'ignored': self.search_count([('exception_bucket', '=', 'ignored')]),
        }

        execution_counts = {
            'pending': _count([('execution_status', '=', 'pending')]),
            'partial': _count([('execution_status', '=', 'partial')]),
            'done': _count([('execution_status', '=', 'done')]),
            'blocked': _count([('execution_status', '=', 'blocked')]),
        }

        health_counts = {
            'out': _count([('coverage_status', '=', 'out')]),
            'critical': _count([('coverage_status', '=', 'critical')]),
            'normal': _count([('coverage_status', '=', 'normal')]),
            'warning': _count([('coverage_status', '=', 'warning')]),
            'ignored': self.search_count([('coverage_status', '=', 'ignored')]),
        }

        setup_counts = {
            'missing_vendor': _count([('setup_issue', '=', 'missing_vendor')]),
            'missing_transfer_source': _count([('setup_issue', '=', 'missing_transfer_source')]),
            'no_rule': _count([('has_policy', '=', False)]),
            'multi_donor': _count([('donor_count', '>', 1)]),
        }

        urgent_count = len(attention_lines)
        buy_budget = _estimate_buy_budget(buy_lines)
        rebalance_qty = round(sum(transfer_lines.mapped('suggested_transfer_qty')), 2)
        rebalance_count = len(transfer_lines)
        setup_blockers = queue_counts['setup']

        warehouse_groups = {}
        for rec in attention_lines.filtered(lambda r: r.warehouse_id):
            warehouse = rec.warehouse_id
            warehouse_group = warehouse_groups.setdefault(warehouse.id, {
                'warehouse_id': warehouse.id,
                'warehouse_name': warehouse.display_name,
                'urgent_lines': 0,
                'buy_budget': 0.0,
                'transfer_qty': 0.0,
                'blocked_lines': 0,
            })
            warehouse_group['urgent_lines'] += 1

            if rec.action_type in ('buy', 'split') and (rec.suggested_buy or 0.0) > 0:
                warehouse_group['buy_budget'] += (rec.suggested_buy or 0.0) * _estimate_unit_cost(rec)

            if rec.action_type in ('transfer', 'split') and (rec.suggested_transfer_qty or 0.0) > 0:
                warehouse_group['transfer_qty'] += rec.suggested_transfer_qty or 0.0

            if rec.exception_bucket == 'setup':
                warehouse_group['blocked_lines'] += 1

        warehouse_pressure = sorted(
            [{
                **item,
                'buy_budget': round(item['buy_budget'], 2),
                'transfer_qty': round(item['transfer_qty'], 2),
            } for item in warehouse_groups.values()],
            key=lambda item: (
                item['urgent_lines'],
                item['buy_budget'],
                item['transfer_qty'],
                item['blocked_lines'],
            ),
            reverse=True,
        )[:5]

        return {
            'title': _('Niyu Planning Overview'),
            'subtitle': _('Forecast status, action queue, blockers, and warehouse pressure in one place.'),
            'can_manage': self.env.user.has_group('niyu_smart_stock.group_niyu_forecast_manager'),
            'last_sync': last_sync_str,
            'sync_status': sync_status,
            'sync_msg': sync_msg,
            'currency_symbol': currency.symbol or '$',
            'currency_position': currency.position or 'before',
            'latest_run': {
                'id': latest_run.id if latest_run else False,
                'name': latest_run.display_name if latest_run else _('No runs yet'),
                'state': latest_run.state if latest_run else False,
                'state_label': latest_run_state_label,
                'message': latest_run.message if latest_run else '',
                'warehouse_count': len(latest_run.warehouse_ids) if latest_run else 0,
                'quota_syncs_left': latest_run.quota_syncs_left if latest_run and latest_run.quota_syncs_left is not False else False,
            },
            'kpis': {
                'urgent_count': urgent_count,
                'buy_budget': buy_budget,
                'buy_line_count': len(buy_lines),
                'rebalance_qty': rebalance_qty,
                'rebalance_count': rebalance_count,
                'setup_blockers': setup_blockers,
            },
            'queue_counts': queue_counts,
            'execution_counts': execution_counts,
            'health_counts': health_counts,
            'setup_counts': setup_counts,
            'warehouse_pressure': warehouse_pressure,
            'subscription': {
                'tier': plan_tier,
                'sku_limit': plan_sku_limit,
                'manual_limit': plan_manual_limit,
                'scheduled_limit': plan_scheduled_limit,
                'max_horizon_days': plan_max_horizon_days,
                'model_type': plan_model_type,
                'seen_at': plan_seen_at,
            },
        }

    def action_open_purchase_orders(self):
        pos = self.mapped('purchase_order_ids')
        if not pos:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('No RFQ Linked'),
                    'message': _('No RFQ or purchase order is linked to the selected action lines yet.'),
                    'type': 'warning',
                    'sticky': False,
                }
            }
        return {
            'name': _('Linked RFQs / Purchase Orders'),
            'type': 'ir.actions.act_window',
            'res_model': 'purchase.order',
            'view_mode': 'list,form',
            'domain': [('id', 'in', pos.ids)],
            'target': 'current',
        }

    def action_open_transfers(self):
        pickings = self.mapped('transfer_picking_ids')
        if not pickings:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('No Transfer Linked'),
                    'message': _('No internal transfer is linked to the selected action lines yet.'),
                    'type': 'warning',
                    'sticky': False,
                }
            }
        return {
            'name': _('Linked Internal Transfers'),
            'type': 'ir.actions.act_window',
            'res_model': 'stock.picking',
            'view_mode': 'list,form',
            'domain': [('id', 'in', pickings.ids)],
            'target': 'current',
        }

    def action_reset_execution_links(self):
        self.write({
            'purchase_order_ids': [(6, 0, [])],
            'transfer_picking_ids': [(6, 0, [])],
            'po_last_action_at': False,
            'transfer_last_action_at': False,
        })
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Execution Links Reset'),
                'message': _('Linked RFQ and transfer references were cleared from the action line. Existing documents were not deleted.'),
                'type': 'success',
                'sticky': False,
            }
        }

    def action_ignore(self):
        self.mapped('transfer_plan_ids').unlink()
        self.write({
            'ignored': True,
            'ignore_source': 'manual',
            'action_type': 'ignore',
            'action_reason': _('Ignored manually by user.'),
            'suggested_transfer_qty': 0.0,
            'transfer_source_warehouse_id': False,
            'transfer_coverage_pct': 0.0,
        })
        return True

    def action_unignore(self):
        for rec in self:
            values = {
                'ignored': False,
                'ignore_source': False,
            }
            if rec.action_type == 'ignore':
                values['action_type'] = 'watch'
            rec.write(values)
        return True

    def action_create_po(self):
        vendor_buckets = {}
        records_to_process = self or self.search([('suggested_buy', '>', 0), ('ignored', '=', False)])
        po_links_by_result = {}
        now = fields.Datetime.now()

        for rec in records_to_process:
            if rec.ignored or rec.action_type == 'transfer' or rec.purchase_order_ids:
                continue

            qty = rec.rounded_order_qty or rec.suggested_buy
            if qty <= 0:
                continue

            vendor = rec._get_preferred_vendor()
            if not vendor:
                continue

            seller = rec._get_supplierinfo_for_vendor(vendor)
            picking_type = rec.warehouse_id.in_type_id if rec.warehouse_id and rec.warehouse_id.in_type_id else False
            bucket_key = (rec.company_id.id, vendor.id, rec.warehouse_id.id if rec.warehouse_id else 0)
            vendor_buckets.setdefault(bucket_key, [])
            vendor_buckets[bucket_key].append((rec, vendor, seller, qty, picking_type))

        if not vendor_buckets:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Nothing to Purchase'),
                    'message': _('No eligible buy lines were found. They may already be linked to RFQs, be ignored, or still need vendor setup.'),
                    'type': 'warning',
                    'sticky': False,
                }
            }

        PurchaseOrder = self.env['purchase.order']
        PurchaseOrderLine = self.env['purchase.order.line']
        pol_uom_field = self._get_purchase_line_uom_field_name()
        created_or_updated_pos = self.env['purchase.order']

        for (company_id, vendor_id, warehouse_id), bucket in vendor_buckets.items():
            vendor = self.env['res.partner'].browse(vendor_id)
            company = self.env['res.company'].browse(company_id)
            warehouse = self.env['stock.warehouse'].browse(warehouse_id) if warehouse_id else False
            picking_type = warehouse.in_type_id if warehouse and warehouse.in_type_id else False

            po_domain = [
                ('partner_id', '=', vendor.id),
                ('company_id', '=', company.id),
                ('state', '=', 'draft'),
            ]
            if picking_type:
                po_domain.append(('picking_type_id', '=', picking_type.id))

            po = PurchaseOrder.search(po_domain, limit=1, order='id desc')

            po_vals = {
                'partner_id': vendor.id,
                'company_id': company.id,
                'origin': 'Niyu Smart Stock',
            }
            if picking_type:
                po_vals['picking_type_id'] = picking_type.id

            if not po:
                po = PurchaseOrder.create(po_vals)
            elif 'Niyu Smart Stock' not in (po.origin or ''):
                po.origin = ((po.origin or '').strip(', ') + ', Niyu Smart Stock').strip(', ')

            for rec, _vendor, seller, qty, _picking_type in bucket:
                uom = rec._get_purchase_uom()
                if not uom:
                    continue

                lead_days = (seller.delay if seller else 0.0) or rec.lead_time_days or 0.0
                date_planned = fields.Datetime.now() + timedelta(days=max(0, int(round(lead_days))))

                def _same_product_line(line):
                    if getattr(line, 'display_type', False):
                        return False
                    if line.product_id.id != rec.product_id.id:
                        return False
                    if not pol_uom_field:
                        return True
                    line_uom = getattr(line, pol_uom_field, False)
                    return not line_uom or line_uom.id == uom.id

                existing_line = po.order_line.filtered(_same_product_line)[:1]

                if existing_line:
                    existing_line.product_qty += qty
                else:
                    line_vals = {
                        'order_id': po.id,
                        'name': rec.product_id.display_name,
                        'product_id': rec.product_id.id,
                        'product_qty': qty,
                        'price_unit': seller.price if seller else 0.0,
                        'date_planned': date_planned,
                    }
                    if pol_uom_field:
                        line_vals[pol_uom_field] = uom.id
                    PurchaseOrderLine.create(line_vals)

                po_links_by_result.setdefault(rec.id, self.env['purchase.order'])
                po_links_by_result[rec.id] |= po

            created_or_updated_pos |= po

        for rec_id, pos in po_links_by_result.items():
            rec = self.browse(rec_id)
            rec.write({
                'purchase_order_ids': [(6, 0, (rec.purchase_order_ids | pos).ids)],
                'po_last_action_at': now,
            })

        return {
            'name': _('Generated RFQs'),
            'type': 'ir.actions.act_window',
            'res_model': 'purchase.order',
            'view_mode': 'list,form',
            'domain': [('id', 'in', created_or_updated_pos.ids)],
            'target': 'current',
        }

    def action_create_transfer(self):
        transfer_buckets = {}
        records_to_process = self or self.search([
            ('action_type', 'in', ['transfer', 'split']),
            ('suggested_transfer_qty', '>', 0),
            ('ignored', '=', False),
        ])
        picking_links_by_result = {}
        now = fields.Datetime.now()

        live_capacity_cache = {}

        def _get_live_transferable(company, product, source_wh):
            key = (company.id, product.id, source_wh.id)
            if key not in live_capacity_cache:
                product_ctx = product.with_company(company).with_context(warehouse=source_wh.id)
                source_free_qty = float(getattr(product_ctx, 'free_qty', 0.0) or 0.0)
                donor_line = self.search([
                    ('company_id', '=', company.id),
                    ('product_id', '=', product.id),
                    ('warehouse_id', '=', source_wh.id),
                ], limit=1)
                protected_qty = donor_line.reorder_point if donor_line else 0.0
                live_capacity_cache[key] = max(0.0, source_free_qty - protected_qty)
            return live_capacity_cache[key]

        for rec in records_to_process:
            if rec.ignored or rec.action_type not in ('transfer', 'split') or rec.transfer_picking_ids:
                continue
            if not rec.transfer_plan_ids or not rec.warehouse_id:
                continue

            for plan in rec.transfer_plan_ids:
                source_wh = plan.source_warehouse_id
                dest_wh = rec.warehouse_id
                if not source_wh or source_wh == dest_wh:
                    continue

                requested_qty = plan.qty or 0.0
                if requested_qty <= 0:
                    continue

                available_qty = _get_live_transferable(rec.company_id, rec.product_id, source_wh)
                qty = min(requested_qty, available_qty)
                if qty <= 0:
                    continue

                live_capacity_cache[(rec.company_id.id, rec.product_id.id, source_wh.id)] = max(0.0, available_qty - qty)

                picking_type = rec._get_internal_picking_type(source_wh)
                if not picking_type or not source_wh.lot_stock_id or not dest_wh.lot_stock_id:
                    continue

                bucket_key = (rec.company_id.id, source_wh.id, dest_wh.id)
                transfer_buckets.setdefault(bucket_key, [])
                transfer_buckets[bucket_key].append((rec, qty, picking_type, source_wh, dest_wh))

        if not transfer_buckets:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Nothing to Transfer'),
                    'message': _('No eligible transfer lines were found. They may already be linked to transfers or donor stock may no longer be available.'),
                    'type': 'warning',
                    'sticky': False,
                }
            }

        Picking = self.env['stock.picking']
        Move = self.env['stock.move']
        move_uom_field = self._get_stock_move_uom_field_name()
        move_text_field = self._get_stock_move_text_field_name()
        created_or_updated_pickings = self.env['stock.picking']

        for (company_id, source_wh_id, dest_wh_id), bucket in transfer_buckets.items():
            company = self.env['res.company'].browse(company_id)
            source_wh = self.env['stock.warehouse'].browse(source_wh_id)
            dest_wh = self.env['stock.warehouse'].browse(dest_wh_id)
            picking_type = bucket[0][2]

            picking_domain = [
                ('company_id', '=', company.id),
                ('picking_type_id', '=', picking_type.id),
                ('location_id', '=', source_wh.lot_stock_id.id),
                ('location_dest_id', '=', dest_wh.lot_stock_id.id),
                ('state', '=', 'draft'),
            ]
            picking = Picking.search(picking_domain, limit=1, order='id desc')

            picking_vals = {
                'company_id': company.id,
                'picking_type_id': picking_type.id,
                'location_id': source_wh.lot_stock_id.id,
                'location_dest_id': dest_wh.lot_stock_id.id,
                'origin': 'Niyu Smart Stock Transfer',
            }

            if not picking:
                picking = Picking.create(picking_vals)
            elif 'Niyu Smart Stock Transfer' not in (picking.origin or ''):
                picking.origin = ((picking.origin or '').strip(', ') + ', Niyu Smart Stock Transfer').strip(', ')

            picking_move_field = 'move_ids_without_package' if 'move_ids_without_package' in Picking._fields else 'move_ids'
            existing_moves = getattr(picking, picking_move_field)

            for rec, qty, _picking_type, _source_wh, _dest_wh in bucket:
                uom = rec.product_id.uom_id or rec.product_id.product_tmpl_id.uom_id
                if not uom:
                    continue

                def _same_move(move):
                    if move.product_id.id != rec.product_id.id:
                        return False
                    if not move_uom_field:
                        return True
                    move_uom = getattr(move, move_uom_field, False)
                    return not move_uom or move_uom.id == uom.id

                existing_move = existing_moves.filtered(_same_move)[:1]

                if existing_move:
                    existing_move.product_uom_qty += qty
                else:
                    move_vals = {
                        'product_id': rec.product_id.id,
                        'product_uom_qty': qty,
                        'picking_id': picking.id,
                        'company_id': rec.company_id.id,
                        'location_id': source_wh.lot_stock_id.id,
                        'location_dest_id': dest_wh.lot_stock_id.id,
                    }
                    if move_uom_field:
                        move_vals[move_uom_field] = uom.id
                    if move_text_field:
                        move_vals[move_text_field] = rec.product_id.display_name
                    Move.create(move_vals)

                picking_links_by_result.setdefault(rec.id, self.env['stock.picking'])
                picking_links_by_result[rec.id] |= picking

            if picking.state == 'draft':
                picking.action_confirm()

            created_or_updated_pickings |= picking

        for rec_id, pickings in picking_links_by_result.items():
            rec = self.browse(rec_id)
            rec.write({
                'transfer_picking_ids': [(6, 0, (rec.transfer_picking_ids | pickings).ids)],
                'transfer_last_action_at': now,
            })

        return {
            'name': _('Generated Internal Transfers'),
            'type': 'ir.actions.act_window',
            'res_model': 'stock.picking',
            'view_mode': 'list,form',
            'domain': [('id', 'in', created_or_updated_pickings.ids)],
            'target': 'current',
        }

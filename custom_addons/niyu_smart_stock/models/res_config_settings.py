from odoo import models, fields


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    niyu_license_key = fields.Char(
        string="Niyu License Key",
        config_parameter='niyu.license_key',
        help="Get your key at niyulabs.com",
    )

    # These are read-only snapshot fields. Char is intentional here so empty
    # values stay blank instead of showing misleading 0 defaults in Settings.
    niyu_plan_tier = fields.Char(
        string="Tier",
        readonly=True,
        config_parameter='niyu.last_tier',
    )
    niyu_plan_sku_limit = fields.Char(
        string="SKU Limit",
        readonly=True,
        config_parameter='niyu.last_sku_limit',
    )
    niyu_plan_manual_limit = fields.Char(
        string="Manual Refresh Max / Day",
        readonly=True,
        config_parameter='niyu.last_manual_limit',
    )
    niyu_plan_scheduled_limit = fields.Char(
        string="Auto Refresh Max / Day",
        readonly=True,
        config_parameter='niyu.last_scheduled_limit',
    )
    niyu_plan_max_horizon_days = fields.Char(
        string="Max Forecast Horizon (Days)",
        readonly=True,
        config_parameter='niyu.last_max_horizon_days',
    )
    niyu_plan_model_type = fields.Char(
        string="Backend Model",
        readonly=True,
        config_parameter='niyu.last_model_type',
    )
    niyu_backend_last_seen = fields.Char(
        string="Backend Snapshot Time",
        readonly=True,
        config_parameter='niyu.last_backend_seen_at',
    )

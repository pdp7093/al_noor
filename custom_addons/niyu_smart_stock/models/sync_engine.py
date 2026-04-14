import requests
import gzip
import json
import logging
from datetime import timedelta

from odoo import models, fields, api, _, release
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

API_ENDPOINT = "https://api.niyulabs.com"


class NiyuSyncEngine(models.AbstractModel):
    _name = 'niyu.sync.engine'
    _description = 'Handles communication with Niyu Cloud'

    def _get_api_endpoint(self):
        return self.env['ir.config_parameter'].sudo().get_param('niyu.api_endpoint', API_ENDPOINT).rstrip('/')

    def _get_module_version(self):
        module = self.env['ir.module.module'].sudo().search([('name', '=', 'niyu_smart_stock')], limit=1)
        return module.installed_version or module.latest_version or 'unknown'

    def _get_odoo_version(self):
        return release.version

    def _set_status(self, status, msg):
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param('niyu.last_sync_status', status)
        ICP.set_param('niyu.last_sync_msg', msg)
        if status in ('error', 'success', 'processing'):
            ICP.set_param('niyu.last_sync_time', fields.Datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    def _sleep_polling_cron(self):
        # Odoo 19 does not allow a cron to modify itself while it is running.
        # Keeping the polling cron awake is safe because an empty queue exits
        # immediately on the next scheduled tick.
        _logger.info("Niyu AI: Polling cron sleep skipped in Odoo 19.")

    def _wake_polling_cron(self):
        cron = self.env.ref('niyu_smart_stock.ir_cron_niyu_forecast_poll', raise_if_not_found=False)
        if cron and not cron.active:
            cron.write({'active': True})

    def _cleanup_stale_jobs(self):
        jobs = self.env['niyu.polling.job'].search([])
        now = fields.Datetime.now()
        for job in jobs:
            elapsed = (now - job.start_time).total_seconds() if job.start_time else 0
            if elapsed > 1800:
                _logger.warning("Niyu AI: Clearing stale polling job %s", job.job_id)
                if job.run_id:
                    job.run_id.write({
                        'state': 'failed',
                        'completed_at': now,
                        'message': 'Polling job timed out and was cleared automatically.',
                    })
                job.unlink()

    def _get_preforecast_excluded_product_ids(self):
        return self.env['niyu.forecast.exclusion'].get_excluded_product_ids(self.env.company)

    def _mark_excluded_lines_ignored(self, product_ids):
        if not product_ids:
            return
        lines = self.env['niyu.forecast.result'].search([
            ('company_id', '=', self.env.company.id),
            ('product_id', 'in', list(product_ids)),
        ])
        if lines:
            lines.write({
                'ignored': True,
                'ignore_source': 'exclusion',
                'action_type': 'ignore',
                'action_reason': 'Excluded from planning by exclusion rule.',
                'suggested_transfer_qty': 0.0,
                'transfer_source_warehouse_id': False,
                'transfer_coverage_pct': 0.0,
            })

    def _get_stock_seed_pairs(self, excluded_product_ids):
        """
        Seed product/warehouse pairs that currently have stock in a warehouse
        even if they have no direct sales history in that warehouse.
        """
        company = self.env.company
        pair_set = set()
        warehouses = self.env['stock.warehouse'].search([('company_id', '=', company.id)])

        for warehouse in warehouses:
            if not warehouse.lot_stock_id:
                continue

            groups = self.env['stock.quant'].read_group(
                domain=[
                    ('company_id', '=', company.id),
                    ('location_id', 'child_of', warehouse.lot_stock_id.id),
                    ('product_id', '!=', False),
                    ('quantity', '!=', 0.0),
                ],
                fields=['product_id'],
                groupby=['product_id'],
                lazy=False,
            )

            for g in groups:
                if not g.get('product_id'):
                    continue
                product_id = g['product_id'][0]
                if product_id in excluded_product_ids:
                    continue
                pair_set.add((product_id, warehouse.id))

        return pair_set

    def _get_open_supply_seed_pairs(self, excluded_product_ids):
        """
        Seed product/warehouse pairs that currently have open purchase supply
        even if they have no direct sales history yet.
        """
        company = self.env.company
        pair_set = set()

        lines = self.env['purchase.order.line'].search([
            ('company_id', '=', company.id),
            ('state', 'in', ['draft', 'sent', 'to approve', 'purchase']),
            ('product_id', '!=', False),
        ])

        for line in lines:
            remaining = max(0.0, (line.product_qty or 0.0) - (line.qty_received or 0.0))
            if remaining <= 0:
                continue

            warehouse = line.order_id.picking_type_id.warehouse_id
            if not warehouse:
                continue

            product_id = line.product_id.id
            if product_id in excluded_product_ids:
                continue

            pair_set.add((product_id, warehouse.id))

        return pair_set

    def _prepare_sales_history(self):
        excluded_product_ids = self._get_preforecast_excluded_product_ids()
        type_field = 'detailed_type' if 'detailed_type' in self.env['product.template']._fields else 'type'

        query = f"""
            SELECT
                l.product_id,
                so.warehouse_id,
                DATE_TRUNC('day', so.date_order)::date as date_val,
                SUM(l.product_uom_qty) as qty
            FROM sale_order_line l
            JOIN sale_order so ON (l.order_id = so.id)
            JOIN product_product pp ON (l.product_id = pp.id)
            JOIN product_template pt ON (pp.product_tmpl_id = pt.id)
            WHERE so.state IN ('sale', 'done')
              AND so.company_id = %s
              AND so.warehouse_id IS NOT NULL
              AND so.date_order >= CURRENT_DATE - INTERVAL '2 years'
              AND pt.{type_field} IN ('product', 'consu')
              AND COALESCE(pt.purchase_ok, TRUE) = TRUE
            GROUP BY 1, 2, 3
        """
        self.env.cr.execute(query, (self.env.company.id,))
        result = self.env.cr.dictfetchall()

        payload = []
        skipped_rows = 0
        warehouse_ids = set()
        existing_pairs = set()

        for r in result:
            if r['product_id'] in excluded_product_ids:
                skipped_rows += 1
                continue

            warehouse_id = r.get('warehouse_id')
            if not warehouse_id:
                continue

            warehouse_ids.add(warehouse_id)
            existing_pairs.add((r['product_id'], warehouse_id))
            payload.append({
                'product_id': r['product_id'],
                'warehouse_id': warehouse_id,
                'date': str(r['date_val']),
                'qty': r['qty'],
            })

        stock_seed_pairs = self._get_stock_seed_pairs(excluded_product_ids)
        supply_seed_pairs = self._get_open_supply_seed_pairs(excluded_product_ids)
        seed_pairs = (stock_seed_pairs | supply_seed_pairs) - existing_pairs

        # Add short zero-history seed so stock-only / supply-only warehouses
        # become valid forecast lines.
        today = fields.Date.today()
        seed_days = 28
        seeded_rows = 0

        for product_id, warehouse_id in sorted(seed_pairs):
            warehouse_ids.add(warehouse_id)
            for i in range(seed_days):
                day = today - timedelta(days=(seed_days - 1 - i))
                payload.append({
                    'product_id': product_id,
                    'warehouse_id': warehouse_id,
                    'date': str(day),
                    'qty': 0.0,
                })
                seeded_rows += 1

        _logger.info(
            "Niyu AI: Prepared %s warehouse-history rows, added %s seed rows, skipped %s rows due to exclusion rules.",
            len(payload), seeded_rows, skipped_rows
        )
        return payload, excluded_product_ids, sorted(list(warehouse_ids))

    @api.model
    def action_start_forecast(self):
        ICP = self.env['ir.config_parameter'].sudo()
        license_key = ICP.get_param('niyu.license_key')

        if not license_key:
            if self.env.context.get('from_cron'):
                _logger.warning("Niyu AI: Daily sync skipped (No API Key).")
                return
            raise UserError(_("Please enter your Niyu API Key in Settings > Inventory."))

        self._cleanup_stale_jobs()

        existing_job = self.env['niyu.polling.job'].search([], limit=1, order='id desc')
        if existing_job:
            elapsed = (fields.Datetime.now() - existing_job.start_time).total_seconds() if existing_job.start_time else 0
            if elapsed < 900:
                if self.env.context.get('from_cron'):
                    _logger.info("Niyu AI: Sync skipped (Already running).")
                    return
                return {
                    'type': 'ir.actions.client',
                    'tag': 'display_notification',
                    'params': {
                        'title': _('Sync in Progress'),
                        'message': _('A forecast is already running. Please wait a moment.'),
                        'type': 'warning',
                        'sticky': False,
                    }
                }
            _logger.warning("Niyu AI: Found stale polling job. Clearing it.")
            if existing_job.run_id:
                existing_job.run_id.write({
                    'state': 'failed',
                    'completed_at': fields.Datetime.now(),
                    'message': 'Stale polling job was cleared before starting a new forecast.',
                })
            existing_job.unlink()

        payload_data, excluded_product_ids, warehouse_ids = self._prepare_sales_history()

        if excluded_product_ids:
            self._mark_excluded_lines_ignored(excluded_product_ids)

        if not payload_data:
            if excluded_product_ids:
                raise UserError(_(
                    "No eligible sales history remains after applying exclusion rules.\n"
                    "Review Exclusions under Niyu AI and remove the overly broad rule."
                ))
            raise UserError(_(
                "No eligible warehouse-level history or stock/open-supply seed data found.\n"
                "Check sales history, warehouse setup, stock, and open POs."
            ))

        horizon_days = 120
        run = self.env['niyu.forecast.run'].create({
            'state': 'draft',
            'company_id': self.env.company.id,
            'horizon_days': horizon_days,
            'schema_version': '3.0',
            'started_at': fields.Datetime.now(),
            'message': 'Preparing and uploading warehouse-aware payload to Niyu Cloud.',
        })
        if warehouse_ids:
            run.write({'warehouse_ids': [(6, 0, warehouse_ids)]})

        payload = {
            'schema_version': '3.0',
            'grain': 'product_warehouse_day',
            'horizon_days': horizon_days,
            'history': payload_data,
        }

        json_str = json.dumps(payload).encode('utf-8')
        compressed_data = gzip.compress(json_str)

        request_source = 'cron' if self.env.context.get('from_cron') else 'manual'
        endpoint = self._get_api_endpoint()

        headers = {
            'X-License-Key': license_key,
            'X-Request-Source': request_source,
            'X-Module-Version': self._get_module_version(),
            'X-Odoo-Version': self._get_odoo_version(),
            'Content-Encoding': 'gzip',
            'Content-Type': 'application/json',
        }

        try:
            req = requests.post(f"{API_ENDPOINT}/forecast", data=compressed_data, headers=headers, timeout=60)

            if req.status_code == 200:
                job_data = req.json()
                job_id = job_data.get('job_id')
                if not job_id:
                    run.write({
                        'state': 'failed',
                        'completed_at': fields.Datetime.now(),
                        'message': 'Cloud response did not include job_id.',
                    })
                    self._set_status('error', 'Cloud Error!')
                    raise UserError(_("Cloud response was incomplete. Missing job ID."))

                self.env['niyu.polling.job'].create({
                    'job_id': job_id,
                    'start_time': fields.Datetime.now(),
                    'run_id': run.id,
                    'state': 'queued',
                })

                excluded_count = len(excluded_product_ids)
                wh_count = len(warehouse_ids)
                extra_msg = []
                if wh_count:
                    extra_msg.append(f'{wh_count} warehouses included')
                if excluded_count:
                    extra_msg.append(f'{excluded_count} products excluded')
                suffix = '. ' + ' • '.join(extra_msg) if extra_msg else ''

                run.write({
                    'job_id': job_id,
                    'state': 'queued',
                    'message': 'Warehouse-aware forecast job accepted by Niyu Cloud and queued for polling' + suffix,
                })

                self._wake_polling_cron()
                self._set_status('processing', 'AI Crunching Data...')
                return {
                    'type': 'ir.actions.client',
                    'tag': 'display_notification',
                    'params': {
                        'title': _('Warehouse Forecast Started'),
                        'message': _('Warehouse-aware forecast started in background. Results will appear after polling completes.'),
                        'type': 'success',
                        'sticky': False,
                    }
                }

            if req.status_code == 429:
                run.write({
                    'state': 'failed',
                    'completed_at': fields.Datetime.now(),
                    'message': req.text or 'Rate limit reached.',
                })
                self._set_status('error', 'Rate Limit Exceeded.')
                try:
                    detail = req.json().get('detail', '')
                except Exception:
                    detail = req.text
                raise UserError(_("Rate Limit Reached. ") + detail)

            if req.status_code == 403:
                run.write({
                    'state': 'failed',
                    'completed_at': fields.Datetime.now(),
                    'message': 'License key rejected by cloud.',
                })
                self._set_status('error', 'API Key Suspended')
                raise UserError(_("Invalid License Key."))

            run.write({
                'state': 'failed',
                'completed_at': fields.Datetime.now(),
                'message': req.text or 'Unexpected cloud error.',
            })
            self._set_status('error', 'Cloud Error!')
            raise UserError(_("Cloud Error: %s") % req.text)

        except Exception as e:
            run.write({
                'state': 'failed',
                'completed_at': fields.Datetime.now(),
                'message': str(e),
            })
            _logger.error("Niyu AI Connection Error: %s", str(e))
            raise UserError(_("Connection Failed: %s") % str(e))
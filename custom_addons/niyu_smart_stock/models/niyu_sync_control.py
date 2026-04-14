import logging
from datetime import timedelta

import requests
from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError

_logger = logging.getLogger(__name__)

API_ENDPOINT_DEFAULT = 'https://api.niyulabs.com'
SYNC_TIMEOUT_MINUTES = 10


class NiyuForecastRunControl(models.Model):
    _inherit = 'niyu.forecast.run'

    state = fields.Selection(
        selection_add=[('stale', 'Stale'), ('expired', 'Expired')],
        ondelete={'stale': 'set default', 'expired': 'set default'},
    )
    request_source = fields.Selection([
        ('manual', 'Manual'),
        ('cron', 'Scheduled Cron'),
    ], string='Started By', default='manual', copy=False)
    remote_status = fields.Char(string='Remote Status', copy=False)
    status_checked_at = fields.Datetime(string='Remote Checked On', copy=False)
    quota_syncs_left = fields.Integer(string='Syncs Left Today', copy=False)
    is_active_remote = fields.Boolean(string='Remote Active', compute='_compute_is_active_remote')

    @api.depends('state')
    def _compute_is_active_remote(self):
        for rec in self:
            rec.is_active_remote = rec.state in ('draft', 'queued', 'running')

    def _get_api_endpoint(self):
        return self.env['ir.config_parameter'].sudo().get_param('niyu.api_endpoint', API_ENDPOINT_DEFAULT).rstrip('/')

    def _get_license_key(self):
        return self.env['ir.config_parameter'].sudo().get_param('niyu.license_key')

    def _get_remote_headers(self):
        headers = {}
        license_key = self._get_license_key()
        if license_key:
            headers['X-License-Key'] = license_key
        return headers

    def _set_sync_banner(self, status, message):
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param('niyu.last_sync_time', fields.Datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        ICP.set_param('niyu.last_sync_status', status)
        ICP.set_param('niyu.last_sync_msg', message or '')


    def _first_payload_value(self, payload, *paths):
        payload = payload or {}
        for path in paths:
            value = payload
            for part in path.split('.'):
                if isinstance(value, dict):
                    value = value.get(part)
                else:
                    value = None
                    break
            if value not in (None, ''):
                return value
        return None

    def _store_subscription_snapshot(self, payload, fetched_at=None):
        payload = payload or {}
        if not isinstance(payload, dict):
            return False

        ICP = self.env['ir.config_parameter'].sudo()
        updated = False

        def set_if_value(param_key, value):
            nonlocal updated
            if value in (None, ''):
                return
            ICP.set_param(param_key, str(value))
            updated = True

        set_if_value('niyu.last_tier', self._first_payload_value(
            payload,
            'tier',
            'plan_tier',
            'subscription.tier',
            'subscription.plan',
            'plan.tier',
        ))
        set_if_value('niyu.last_sku_limit', self._first_payload_value(
            payload,
            'sku_limit',
            'plan_sku_limit',
            'subscription.sku_limit',
            'subscription.skus',
            'limits.sku_limit',
        ))
        set_if_value('niyu.last_manual_limit', self._first_payload_value(
            payload,
            'manual_limit',
            'manual_refresh_limit',
            'manual_refresh_max_per_day',
            'subscription.manual_limit',
            'limits.manual',
            'max_syncs_day',
        ))
        set_if_value('niyu.last_scheduled_limit', self._first_payload_value(
            payload,
            'scheduled_limit',
            'scheduled_refresh_limit',
            'auto_refresh_limit',
            'auto_refresh_max_per_day',
            'subscription.scheduled_limit',
            'limits.scheduled',
        ))
        set_if_value('niyu.last_max_horizon_days', self._first_payload_value(
            payload,
            'max_horizon_days',
            'forecast_horizon_days',
            'horizon_days',
            'subscription.max_horizon_days',
            'limits.max_horizon_days',
        ))
        set_if_value('niyu.last_model_type', self._first_payload_value(
            payload,
            'model_type',
            'backend_model',
            'model',
            'subscription.model_type',
        ))

        seen_at = self._first_payload_value(
            payload,
            'backend_seen_at',
            'snapshot_time',
            'snapshot_at',
            'seen_at',
            'generated_at',
            'checked_at',
        ) or fetched_at or fields.Datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        ICP.set_param('niyu.last_backend_seen_at', str(seen_at))
        return updated

    def _apply_remote_status_payload(self, payload):
        self.ensure_one()
        remote_status = (payload or {}).get('status') or ''
        message = (payload or {}).get('message') or ''
        syncs_left = int((payload or {}).get('manual_left_today') or 0)
        now = fields.Datetime.now()
        self.env['niyu.sync.engine']._store_subscription_snapshot(payload, fetched_at=now.strftime('%Y-%m-%d %H:%M:%S'))
        vals = {
            'remote_status': remote_status,
            'status_checked_at': now,
            'quota_syncs_left': syncs_left,
        }

        if remote_status in ('queued', 'running', 'processing'):
            vals['state'] = 'running' if remote_status != 'queued' else 'queued'
            if not self.started_at:
                vals['started_at'] = now
            if message:
                vals['message'] = message
        elif remote_status == 'done':
            # Only mark the run done after Odoo has actually fetched /result
            # and written the new action-line snapshot locally.
            has_live_job = bool(self.env['niyu.polling.job'].sudo().search_count([('run_id', '=', self.id)]))
            if not has_live_job and self.state not in ('draft', 'queued', 'running'):
                vals['state'] = 'done'
            if message:
                vals['message'] = message
        elif remote_status == 'failed':
            vals['state'] = 'failed'
            vals['completed_at'] = self.completed_at or now
            vals['message'] = message or _('Remote forecast failed.')
        elif remote_status == 'stale':
            vals['state'] = 'stale'
            vals['completed_at'] = self.completed_at or now
            vals['message'] = message or _('Remote forecast became stale and is now ready for rerun.')
        elif remote_status == 'expired':
            vals['state'] = 'expired'
            vals['completed_at'] = self.completed_at or now
            vals['message'] = message or _('Remote forecast result has expired from storage. Run sync again to request a new one.')

        self.write(vals)
        return vals

    def action_refresh_remote_status(self):
        if not self.env.user.has_group('niyu_smart_stock.group_niyu_forecast_user'):
            raise AccessError(_('You do not have access to view Niyu AI forecast runs.'))

        headers = self._get_remote_headers()
        if not headers.get('X-License-Key'):
            raise UserError(_('Missing Niyu license key in system settings.'))

        endpoint = self._get_api_endpoint()
        refreshed = 0
        for run in self.filtered(lambda r: r.job_id):
            try:
                resp = requests.get(
                    f"{endpoint}/status/{run.job_id}",
                    headers=headers,
                    timeout=15,
                )
                if resp.status_code == 200:
                    payload = resp.json()
                    run._apply_remote_status_payload(payload)
                    refreshed += 1
                else:
                    _logger.warning('Niyu AI: Status refresh returned %s for run %s', resp.status_code, run.id)
            except Exception as exc:
                _logger.exception('Niyu AI: Failed refreshing remote status for run %s: %s', run.id, exc)

        if refreshed:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Remote Status Updated'),
                    'message': _('%s run(s) refreshed from the forecasting backend.') % refreshed,
                    'type': 'success',
                    'sticky': False,
                }
            }
        return True


class NiyuForecastResultControl(models.Model):
    _inherit = 'niyu.forecast.result'

    def _ensure_forecast_manager(self):
        if not self.env.user.has_group('niyu_smart_stock.group_niyu_forecast_manager'):
            raise AccessError(_('Only Niyu Forecast Managers can change forecast actions.'))

    def action_create_po(self):
        self._ensure_forecast_manager()
        return super().action_create_po()

    def action_create_transfer(self):
        self._ensure_forecast_manager()
        return super().action_create_transfer()

    def action_ignore(self):
        self._ensure_forecast_manager()
        return super().action_ignore()

    def action_unignore(self):
        self._ensure_forecast_manager()
        return super().action_unignore()

    def action_reset_execution_links(self):
        self._ensure_forecast_manager()
        return super().action_reset_execution_links()


class NiyuForecastSyncWizard(models.TransientModel):
    _name = 'niyu.forecast.sync.wizard'
    _description = 'Niyu Forecast Sync Confirmation'

    tier = fields.Char(readonly=True)
    sku_limit = fields.Integer(readonly=True)
    max_syncs_day = fields.Integer(string='Daily Sync Limit', readonly=True)
    syncs_used_today = fields.Integer(readonly=True)
    manual_left_today = fields.Integer(readonly=True)
    active_job_id = fields.Char(string='Active Remote Job', readonly=True)
    active_job_status = fields.Char(string='Active Remote Status', readonly=True)
    latest_run_id = fields.Many2one('niyu.forecast.run', string='Latest Run', readonly=True)
    latest_run_state = fields.Char(string='Latest Run State', readonly=True)
    latest_run_message = fields.Text(readonly=True)
    backend_message = fields.Text(readonly=True)
    confirmation_note = fields.Html(string='Confirmation', sanitize=False, readonly=True)

    def action_confirm_sync(self):
        self.ensure_one()
        if not self.env.user.has_group('niyu_smart_stock.group_niyu_forecast_manager'):
            raise AccessError(_('Only Niyu Forecast Managers can run a new sync.'))

        if self.active_job_id and self.latest_run_id and self.latest_run_id.state in ('queued', 'running'):
            return {
                'name': _('Forecast Run'),
                'type': 'ir.actions.act_window',
                'res_model': 'niyu.forecast.run',
                'view_mode': 'form',
                'res_id': self.latest_run_id.id,
                'target': 'current',
            }

        if self.max_syncs_day and self.manual_left_today <= 0:
            raise UserError(_('Daily sync limit reached for this subscription. Try again tomorrow or upgrade the plan.'))

        return self.env['niyu.sync.engine'].with_context(niyu_manual_sync=True).action_start_forecast()

    def action_open_latest_run(self):
        self.ensure_one()
        if not self.latest_run_id:
            return {'type': 'ir.actions.act_window_close'}
        return {
            'name': _('Forecast Run'),
            'type': 'ir.actions.act_window',
            'res_model': 'niyu.forecast.run',
            'view_mode': 'form',
            'res_id': self.latest_run_id.id,
            'target': 'current',
        }


class NiyuSyncEngineControl(models.AbstractModel):
    _inherit = 'niyu.sync.engine'

    def _get_api_endpoint(self):
        return self.env['ir.config_parameter'].sudo().get_param('niyu.api_endpoint', API_ENDPOINT_DEFAULT).rstrip('/')

    def _get_license_key(self):
        return self.env['ir.config_parameter'].sudo().get_param('niyu.license_key')

    def _get_remote_headers(self):
        headers = {}
        license_key = self._get_license_key()
        if license_key:
            headers['X-License-Key'] = license_key
        return headers

    def _set_sync_banner(self, status, message):
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param('niyu.last_sync_time', fields.Datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        ICP.set_param('niyu.last_sync_status', status)
        ICP.set_param('niyu.last_sync_msg', message or '')


    def _first_payload_value(self, payload, *paths):
        payload = payload or {}
        for path in paths:
            value = payload
            for part in path.split('.'):
                if isinstance(value, dict):
                    value = value.get(part)
                else:
                    value = None
                    break
            if value not in (None, ''):
                return value
        return None

    def _store_subscription_snapshot(self, payload, fetched_at=None):
        payload = payload or {}
        if not isinstance(payload, dict):
            return False

        ICP = self.env['ir.config_parameter'].sudo()
        updated = False

        def set_if_value(param_key, value):
            nonlocal updated
            if value in (None, ''):
                return
            ICP.set_param(param_key, str(value))
            updated = True

        set_if_value('niyu.last_tier', self._first_payload_value(
            payload,
            'tier',
            'plan_tier',
            'subscription.tier',
            'subscription.plan',
            'plan.tier',
        ))
        set_if_value('niyu.last_sku_limit', self._first_payload_value(
            payload,
            'sku_limit',
            'plan_sku_limit',
            'subscription.sku_limit',
            'subscription.skus',
            'limits.sku_limit',
        ))
        set_if_value('niyu.last_manual_limit', self._first_payload_value(
            payload,
            'manual_limit',
            'manual_refresh_limit',
            'manual_refresh_max_per_day',
            'subscription.manual_limit',
            'limits.manual',
            'max_syncs_day',
        ))
        set_if_value('niyu.last_scheduled_limit', self._first_payload_value(
            payload,
            'scheduled_limit',
            'scheduled_refresh_limit',
            'auto_refresh_limit',
            'auto_refresh_max_per_day',
            'subscription.scheduled_limit',
            'limits.scheduled',
        ))
        set_if_value('niyu.last_max_horizon_days', self._first_payload_value(
            payload,
            'max_horizon_days',
            'forecast_horizon_days',
            'horizon_days',
            'subscription.max_horizon_days',
            'limits.max_horizon_days',
        ))
        set_if_value('niyu.last_model_type', self._first_payload_value(
            payload,
            'model_type',
            'backend_model',
            'model',
            'subscription.model_type',
        ))

        seen_at = self._first_payload_value(
            payload,
            'backend_seen_at',
            'snapshot_time',
            'snapshot_at',
            'seen_at',
            'generated_at',
            'checked_at',
        ) or fetched_at or fields.Datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        ICP.set_param('niyu.last_backend_seen_at', str(seen_at))
        return updated

    def _get_latest_run(self):
        return self.env['niyu.forecast.run'].search([], order='id desc', limit=1)

    def _get_latest_polling_job(self):
        return self.env['niyu.polling.job'].search([], order='id desc', limit=1)

    def _get_live_polling_job(self):
        jobs = self.env['niyu.polling.job'].search([('state', 'in', ['queued', 'running'])], order='start_time desc, id desc')
        now = fields.Datetime.now()
        for job in jobs:
            start_time = job.start_time or job.create_date or now
            age_seconds = (now - start_time).total_seconds()
            if age_seconds <= (SYNC_TIMEOUT_MINUTES * 60):
                return job
        return self.env['niyu.polling.job']

    def _mark_stale_local_jobs(self):
        stale_before = fields.Datetime.now() - timedelta(minutes=SYNC_TIMEOUT_MINUTES)
        stale_jobs = self.env['niyu.polling.job'].search([
            ('state', 'in', ['queued', 'running']),
            ('start_time', '!=', False),
            ('start_time', '<', stale_before),
        ])
        for job in stale_jobs:
            if job.run_id:
                job.run_id.write({
                    'state': 'stale',
                    'completed_at': fields.Datetime.now(),
                    'message': _('Marked stale after %s minutes without a terminal response from backend.') % SYNC_TIMEOUT_MINUTES,
                })
            job.unlink()
        if stale_jobs:
            self._set_sync_banner('warning', _('Last remote run became stale and was unlocked for rerun.'))
        return len(stale_jobs)

    def _fetch_remote_quota(self):
        headers = self._get_remote_headers()
        endpoint = self._get_api_endpoint()
        if not headers.get('X-License-Key'):
            return {
                'tier': 'unknown',
                'sku_limit': 0,
                'max_syncs_day': 0,
                'syncs_used_today': 0,
                'manual_left_today': 0,
                'message': _('License key is missing. Configure it before running sync.'),
            }
        try:
            resp = requests.get(f"{endpoint}/quota", headers=headers, timeout=15)
            if resp.status_code == 200:
                payload = resp.json()
                self._store_subscription_snapshot(payload)
                return payload
            return {
                'tier': 'unknown',
                'sku_limit': 0,
                'max_syncs_day': 0,
                'syncs_used_today': 0,
                'manual_left_today': 0,
                'message': _('Backend quota check returned HTTP %s.') % resp.status_code,
            }
        except Exception as exc:
            _logger.exception('Niyu AI: quota fetch failed: %s', exc)
            return {
                'tier': 'unknown',
                'sku_limit': 0,
                'max_syncs_day': 0,
                'syncs_used_today': 0,
                'manual_left_today': 0,
                'message': _('Backend quota check failed. Sync can still be attempted if local state is clear.'),
            }

    @api.model
    def action_open_sync_wizard(self):
        if not self.env.user.has_group('niyu_smart_stock.group_niyu_forecast_manager'):
            raise AccessError(_('Only Niyu Forecast Managers can run a forecast sync.'))

        self._mark_stale_local_jobs()
        quota = self._fetch_remote_quota()
        latest_run = self._get_latest_run()
        live_job = self._get_live_polling_job()

        latest_state = latest_run.state if latest_run else ''
        latest_message = latest_run.message if latest_run else ''
        active_job_id = quota.get('active_job_id') or (live_job and live_job.job_id) or ''
        active_job_status = quota.get('active_job_status') or (live_job and live_job.state) or ''
        syncs_left = int(quota.get('manual_left_today') or 0)
        max_syncs_day = int(quota.get('max_syncs_day') or 0)
        used_today = int(quota.get('syncs_used_today') or 0)
        tier = quota.get('tier') or 'unknown'
        sku_limit = int(quota.get('sku_limit') or 0)

        lines = [
            _('<p><strong>Plan:</strong> %s &nbsp;&nbsp; <strong>SKU limit:</strong> %s</p>') % (tier.title(), sku_limit or '-'),
            _('<p><strong>Daily syncs:</strong> %s used / %s total &nbsp;&nbsp; <strong>Left today:</strong> %s</p>') % (used_today, max_syncs_day or '-', syncs_left),
        ]
        if active_job_id:
            lines.append(_('<p><strong>Active backend job:</strong> %s (%s)</p>') % (active_job_id, active_job_status or _('processing')))
        if latest_run:
            lines.append(_('<p><strong>Latest local run:</strong> %s — %s</p>') % (latest_run.display_name, latest_state))
        backend_message = quota.get('message') or ''
        if backend_message:
            lines.append(_('<p><strong>Backend:</strong> %s</p>') % backend_message)
        lines.append(_('<p>This action may consume one of today\'s allowed syncs. Continue?</p>'))

        wizard = self.env['niyu.forecast.sync.wizard'].create({
            'tier': tier,
            'sku_limit': sku_limit,
            'max_syncs_day': max_syncs_day,
            'syncs_used_today': used_today,
            'manual_left_today': syncs_left,
            'active_job_id': active_job_id,
            'active_job_status': active_job_status,
            'latest_run_id': latest_run.id if latest_run else False,
            'latest_run_state': latest_state,
            'latest_run_message': latest_message,
            'backend_message': backend_message,
            'confirmation_note': ''.join(lines),
        })
        return {
            'name': _('Run Forecast Sync'),
            'type': 'ir.actions.act_window',
            'res_model': 'niyu.forecast.sync.wizard',
            'view_mode': 'form',
            'res_id': wizard.id,
            'target': 'new',
        }

    @api.model
    def action_start_forecast(self):
        self._mark_stale_local_jobs()

        if not self.env.context.get('niyu_manual_sync'):
            self._fetch_remote_quota()

        live_job = self._get_live_polling_job()
        if live_job:
            if live_job.run_id:
                live_job.run_id.action_refresh_remote_status()
                self._set_sync_banner('running', _('Forecast is already running. Opening the active run.'))
                return {
                    'name': _('Forecast Run'),
                    'type': 'ir.actions.act_window',
                    'res_model': 'niyu.forecast.run',
                    'view_mode': 'form',
                    'res_id': live_job.run_id.id,
                    'target': 'current',
                }
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Forecast In Progress'),
                    'message': _('A forecast is already running. Please wait for it to finish.'),
                    'type': 'warning',
                    'sticky': False,
                }
            }

        self._set_sync_banner('running', _('Forecast request sent to backend.'))
        result = super().action_start_forecast()

        latest_poll = self._get_latest_polling_job()
        latest_run = latest_poll.run_id if latest_poll else self._get_latest_run()
        request_source = 'manual' if self.env.context.get('niyu_manual_sync') else 'cron'
        if latest_run:
            vals = {
                'request_source': request_source,
            }
            if latest_run.state == 'draft':
                vals['state'] = 'queued'
            if not latest_run.started_at:
                vals['started_at'] = fields.Datetime.now()
            latest_run.write(vals)

        poll_cron = self.env.ref('niyu_smart_stock.ir_cron_niyu_forecast_poll', raise_if_not_found=False)
        if poll_cron and not poll_cron.active:
            poll_cron.write({'active': True})

        if self.env.context.get('niyu_manual_sync') and latest_run:
            return {
                'name': _('Forecast Run'),
                'type': 'ir.actions.act_window',
                'res_model': 'niyu.forecast.run',
                'view_mode': 'form',
                'res_id': latest_run.id,
                'target': 'current',
            }
        return result


class NiyuPollingJobControl(models.Model):
    _inherit = 'niyu.polling.job'

    def _get_api_endpoint(self):
        return self.env['ir.config_parameter'].sudo().get_param('niyu.api_endpoint', API_ENDPOINT_DEFAULT).rstrip('/')

    def _headers(self):
        license_key = self.env['ir.config_parameter'].sudo().get_param('niyu.license_key')
        return {'X-License-Key': license_key} if license_key else {}

    def _mark_job_terminal(self, job, state, message):
        now = fields.Datetime.now()
        if job.run_id:
            job.run_id.write({
                'state': state,
                'completed_at': job.run_id.completed_at or now,
                'status_checked_at': now,
                'remote_status': state,
                'message': message,
            })
        if state == 'done':
            self._set_global_status('success', _('Forecast completed successfully.'))
        elif state in ('failed', 'stale', 'expired'):
            self._set_global_status('warning', message)
        job.unlink()

    @api.model
    def cron_check_results(self):
        jobs = self.search([], order='id asc')
        if not jobs:
            self._sleep_polling_cron()
            return

        headers = self._headers()
        endpoint = self._get_api_endpoint()
        now = fields.Datetime.now()

        for job in jobs:
            try:
                if job.run_id and job.run_id.state in ('draft', 'queued'):
                    job.run_id.write({'state': 'running', 'started_at': job.run_id.started_at or now})
                if job.state == 'queued':
                    job.state = 'running'

                job_age_seconds = 0
                if job.start_time:
                    job_age_seconds = (now - job.start_time).total_seconds()
                if job_age_seconds > (SYNC_TIMEOUT_MINUTES * 60):
                    self._mark_job_terminal(
                        job,
                        'stale',
                        _('Polling timed out after %s minutes. A new sync can now be started.') % SYNC_TIMEOUT_MINUTES,
                    )
                    continue

                status_resp = requests.get(f"{endpoint}/status/{job.job_id}", headers=headers, timeout=15)
                if status_resp.status_code != 200:
                    _logger.warning('Niyu AI: Status API returned %s for job %s', status_resp.status_code, job.job_id)
                    continue

                payload = status_resp.json()
                self.env['niyu.sync.engine']._store_subscription_snapshot(payload, fetched_at=now.strftime('%Y-%m-%d %H:%M:%S'))
                remote_status = payload.get('status') or 'processing'
                message = payload.get('message') or ''
                if job.run_id:
                    job.run_id.write({
                        'remote_status': remote_status,
                        'status_checked_at': now,
                        'quota_syncs_left': int(payload.get('manual_left_today') or 0),
                    })

                if remote_status in ('queued', 'running', 'processing'):
                    continue

                if remote_status == 'done':
                    result_resp = requests.get(f"{endpoint}/result/{job.job_id}", headers=headers, timeout=30)
                    if result_resp.status_code != 200:
                        _logger.warning('Niyu AI: Result API returned %s for job %s', result_resp.status_code, job.job_id)
                        continue
                    result_payload = result_resp.json()
                    self.env['niyu.sync.engine']._store_subscription_snapshot(result_payload, fetched_at=now.strftime('%Y-%m-%d %H:%M:%S'))
                    result_status = result_payload.get('status')
                    if result_status == 'done':
                        self._process_results(result_payload.get('data', []), job.run_id)
                        self._mark_job_terminal(
                            job,
                            'done',
                            _('Warehouse-aware forecast results received and written to Odoo.'),
                        )
                        continue
                    if result_status == 'expired':
                        self._mark_job_terminal(
                            job,
                            'expired',
                            _('Forecast result expired from backend storage before it could be fetched again.'),
                        )
                        continue
                    continue

                if remote_status in ('failed', 'stale', 'expired'):
                    self._mark_job_terminal(job, remote_status, message or _('Remote job ended with state: %s') % remote_status)
                    continue

            except Exception as exc:
                _logger.exception('Niyu AI Polling Error for %s: %s', job.job_id, exc)

        if not self.search_count([]):
            self._sleep_polling_cron()


class ResUsersNiyuAccess(models.Model):
    _inherit = 'res.users'

    niyu_forecast_access = fields.Selection([
        ('none', 'No Access'),
        ('user', 'User'),
        ('manager', 'Manager'),
    ], string='Niyu Forecast Access', compute='_compute_niyu_forecast_access', inverse='_inverse_niyu_forecast_access')

    def _get_niyu_groups(self):
        user_group = self.env.ref('niyu_smart_stock.group_niyu_forecast_user', raise_if_not_found=False)
        manager_group = self.env.ref('niyu_smart_stock.group_niyu_forecast_manager', raise_if_not_found=False)
        return user_group, manager_group

    def _compute_niyu_forecast_access(self):
        user_group, manager_group = self._get_niyu_groups()
        manager_user_ids = set(manager_group.sudo().user_ids.ids) if manager_group else set()
        basic_user_ids = set(user_group.sudo().user_ids.ids) if user_group else set()
        for user in self:
            if user.id in manager_user_ids:
                user.niyu_forecast_access = 'manager'
            elif user.id in basic_user_ids:
                user.niyu_forecast_access = 'user'
            else:
                user.niyu_forecast_access = 'none'

    def _inverse_niyu_forecast_access(self):
        user_group, manager_group = self._get_niyu_groups()
        if not user_group or not manager_group:
            return
        for user in self:
            # Remove from both groups first, then add the selected one.
            user_group.sudo().write({'user_ids': [(3, user.id)]})
            manager_group.sudo().write({'user_ids': [(3, user.id)]})
            if user.niyu_forecast_access == 'user':
                user_group.sudo().write({'user_ids': [(4, user.id)]})
            elif user.niyu_forecast_access == 'manager':
                manager_group.sudo().write({'user_ids': [(4, user.id)]})

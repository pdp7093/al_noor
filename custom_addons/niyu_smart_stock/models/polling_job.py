import json
import logging
import math
import requests
from odoo import models, fields, api

_logger = logging.getLogger(__name__)

API_ENDPOINT = "https://api.niyulabs.com"


class NiyuPollingJob(models.Model):
    _name = 'niyu.polling.job'
    _description = 'Async Forecast Poller'

    job_id = fields.Char(required=True, index=True)
    start_time = fields.Datetime()
    run_id = fields.Many2one('niyu.forecast.run', string='Forecast Run', ondelete='cascade', index=True)
    state = fields.Selection([
        ('queued', 'Queued'),
        ('running', 'Running'),
        ('done', 'Done'),
        ('failed', 'Failed'),
    ], default='queued', index=True)

    def _sleep_polling_cron(self):
        # Odoo 19 blocks modifying a cron while that cron is executing.
        # Keep the poll cron enabled and simply let empty ticks exit fast.
        _logger.info("Niyu AI: No polling jobs left. Poll cron will stay enabled and exit immediately when idle.")
        return True

    def _set_global_status(self, status, msg):
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param('niyu.last_sync_time', fields.Datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        ICP.set_param('niyu.last_sync_status', status)
        ICP.set_param('niyu.last_sync_msg', msg)

    def _get_default_vendor(self, product):
        seller = product.seller_ids[:1]
        return seller.partner_id if seller else False

    def _category_matches(self, product, category):
        if not category:
            return True
        prod_cat = product.categ_id
        if not prod_cat:
            return False
        return prod_cat.id == category.id or (
            prod_cat.parent_path and category.parent_path and prod_cat.parent_path.startswith(category.parent_path)
        )

    def _select_policy(self, product, company, warehouse=False, vendor=False):
        policies = self.env['niyu.replenishment.policy'].search([
            ('active', '=', True),
            ('company_id', '=', company.id),
        ], order='priority desc, id desc')

        best_policy = self.env['niyu.replenishment.policy']
        best_rank = (-1, -1, -1)

        for policy in policies:
            if policy.warehouse_id and (not warehouse or policy.warehouse_id.id != warehouse.id):
                continue
            if policy.vendor_id and (not vendor or policy.vendor_id.id != vendor.id):
                continue

            specificity = 0

            if policy.product_id:
                if policy.product_id.id != product.id:
                    continue
                specificity += 100
            elif policy.product_tmpl_id:
                if policy.product_tmpl_id.id != product.product_tmpl_id.id:
                    continue
                specificity += 80
            elif policy.categ_id:
                if not self._category_matches(product, policy.categ_id):
                    continue
                specificity += 60

            if policy.vendor_id:
                specificity += 20
            if policy.warehouse_id:
                specificity += 10

            rank = (specificity, policy.priority, policy.id)
            if rank > best_rank:
                best_rank = rank
                best_policy = policy

        return best_policy

    def _compute_incoming_po_qty(self, product, company, warehouse=False, vendor=False, include_drafts=False):
        domain = [
            ('company_id', '=', company.id),
            ('product_id', '=', product.id),
            ('state', 'in', ['purchase', 'to approve'] + (['draft', 'sent'] if include_drafts else [])),
        ]
        if vendor:
            domain.append(('order_id.partner_id', '=', vendor.id))
        if warehouse:
            domain.append(('order_id.picking_type_id.warehouse_id', '=', warehouse.id))

        lines = self.env['purchase.order.line'].search(domain)
        qty = 0.0
        for line in lines:
            remaining = max(0.0, (line.product_qty or 0.0) - (line.qty_received or 0.0))
            qty += remaining
        return qty

    def _round_qty(self, qty, policy):
        qty = max(0.0, qty or 0.0)
        if qty <= 0:
            return 0.0

        fixed_order_qty = policy.fixed_order_qty if policy else 0.0
        qty_multiple = policy.qty_multiple if policy else 0.0
        min_order_qty = policy.min_order_qty if policy else 0.0

        step = 0.0
        if fixed_order_qty and fixed_order_qty > 0:
            step = fixed_order_qty
        elif qty_multiple and qty_multiple > 1:
            step = qty_multiple

        if step > 0:
            qty = math.ceil(qty / step) * step

        if min_order_qty and min_order_qty > 0:
            qty = max(qty, min_order_qty)
            if step > 0:
                qty = math.ceil(qty / step) * step

        return qty

    def _ensure_single_current_line(self, company, product, warehouse=False):
        domain = [
            ('company_id', '=', company.id),
            ('product_id', '=', product.id),
        ]
        if warehouse:
            domain.append(('warehouse_id', '=', warehouse.id))
        else:
            domain.append(('warehouse_id', '=', False))

        records = self.env['niyu.forecast.result'].search(domain, order='id desc')
        if len(records) > 1:
            records[1:].unlink()
        return records[:1]

    def _cleanup_legacy_company_level_lines(self, company, product):
        legacy_lines = self.env['niyu.forecast.result'].search([
            ('company_id', '=', company.id),
            ('product_id', '=', product.id),
            ('warehouse_id', '=', False),
        ])
        if legacy_lines:
            legacy_lines.unlink()

    def _get_stock_snapshot(self, product, company, warehouse=False):
        product_ctx = product.with_company(company)
        if warehouse:
            product_ctx = product_ctx.with_context(warehouse=warehouse.id)

        current_stock = float(getattr(product_ctx, 'qty_available', 0.0) or 0.0)
        free_qty = float(getattr(product_ctx, 'free_qty', current_stock) or 0.0)
        incoming_total = float(getattr(product_ctx, 'incoming_qty', 0.0) or 0.0)
        reserved_qty = max(0.0, current_stock - free_qty)

        return {
            'current_stock': current_stock,
            'free_qty': free_qty,
            'reserved_qty': reserved_qty,
            'incoming_total': incoming_total,
        }

    def _build_action_reason(self, warehouse, forecast_30d, current_stock, incoming_po_qty, incoming_transfer_qty, reorder_point, target_stock, rounded_order_qty):
        warehouse_txt = warehouse.display_name if warehouse else 'Global'
        if rounded_order_qty > 0:
            return (
                f"[{warehouse_txt}] Buy suggested because net available is at/below reorder point. "
                f"Forecast 30d={forecast_30d:.2f}, current stock={current_stock:.2f}, "
                f"incoming PO={incoming_po_qty:.2f}, incoming transfer={incoming_transfer_qty:.2f}, "
                f"reorder point={reorder_point:.2f}, target stock={target_stock:.2f}, "
                f"rounded order qty={rounded_order_qty:.2f}."
            )
        return (
            f"[{warehouse_txt}] No immediate purchase suggested. "
            f"Forecast 30d={forecast_30d:.2f}, current stock={current_stock:.2f}, "
            f"incoming PO={incoming_po_qty:.2f}, incoming transfer={incoming_transfer_qty:.2f}, "
            f"reorder point={reorder_point:.2f}."
        )

    def _apply_transfer_rebalancing(self, company, product_ids):
        if not product_ids:
            return

        TransferPlan = self.env['niyu.forecast.transfer.plan']

        for product_id in product_ids:
            lines = self.env['niyu.forecast.result'].search([
                ('company_id', '=', company.id),
                ('product_id', '=', product_id),
                ('warehouse_id', '!=', False),
            ])

            lines = lines.filtered(lambda l: not l.ignored and l.warehouse_id)
            if len(lines) < 2:
                continue

            # Clear previous donor allocations for this product before rebuilding.
            TransferPlan.search([
                ('company_id', '=', company.id),
                ('product_id', '=', product_id),
            ]).unlink()

            donors = []
            for line in lines:
                donor_surplus = max(0.0, ((line.current_stock or 0.0) - (line.reserved_qty or 0.0)) - (line.reorder_point or 0.0))
                if donor_surplus > 0:
                    donors.append({
                        'line': line,
                        'surplus': donor_surplus,
                        'source_available_qty': max(0.0, (line.current_stock or 0.0) - (line.reserved_qty or 0.0)),
                        'source_protected_qty': line.reorder_point or 0.0,
                    })

            if not donors:
                continue

            receivers = lines.filtered(
                lambda l: (l.rounded_order_qty or l.suggested_buy or 0.0) > 0 and l.action_type in ('buy', 'watch', 'split')
            )
            receivers = receivers.sorted(lambda r: r.rounded_order_qty or r.suggested_buy or 0.0, reverse=True)

            for receiver in receivers:
                original_need = max(receiver.rounded_order_qty or receiver.suggested_buy or 0.0, 0.0)
                if original_need <= 0:
                    continue

                remaining_need = original_need
                allocations = []

                for donor in sorted(donors, key=lambda d: d['surplus'], reverse=True):
                    donor_line = donor['line']
                    if donor_line.warehouse_id == receiver.warehouse_id:
                        continue
                    if donor['surplus'] <= 0:
                        continue
                    if remaining_need <= 0:
                        break

                    alloc_qty = round(min(donor['surplus'], remaining_need), 2)
                    if alloc_qty <= 0:
                        continue

                    donor['surplus'] = max(0.0, donor['surplus'] - alloc_qty)
                    remaining_need = round(max(0.0, remaining_need - alloc_qty), 2)

                    allocations.append({
                        'source_warehouse_id': donor_line.warehouse_id.id,
                        'qty': alloc_qty,
                        'coverage_pct': round((alloc_qty / original_need) * 100.0, 2) if original_need else 0.0,
                        'source_available_qty': donor['source_available_qty'],
                        'source_protected_qty': donor['source_protected_qty'],
                        'source_remaining_after': round(donor['surplus'], 2),
                    })

                total_transfer = round(sum(a['qty'] for a in allocations), 2)
                if total_transfer <= 0:
                    continue

                remaining_buy_qty = round(max(0.0, original_need - total_transfer), 2)
                total_coverage_pct = round((total_transfer / original_need) * 100.0, 2) if original_need else 0.0

                first_source_wh = self.env['stock.warehouse'].browse(allocations[0]['source_warehouse_id'])
                source_text = ' + '.join([
                    '%s %.2f' % (self.env['stock.warehouse'].browse(a['source_warehouse_id']).display_name, a['qty'])
                    for a in allocations
                ])

                if remaining_buy_qty > 0:
                    action_type = 'split'
                    action_reason = (
                        f"[{receiver.warehouse_id.display_name}] Transfer from multiple donors ({source_text}) "
                        f"and buy remaining {remaining_buy_qty:.2f}. Transfer covers {total_coverage_pct:.2f}% of the shortage."
                    )
                else:
                    action_type = 'transfer'
                    action_reason = (
                        f"[{receiver.warehouse_id.display_name}] Fully covered by transfers from multiple donors ({source_text}). "
                        f"Transfer covers 100.00% of the shortage."
                    )

                receiver.write({
                    'action_type': action_type,
                    'suggested_transfer_qty': total_transfer,
                    'transfer_source_warehouse_id': first_source_wh.id if first_source_wh else False,
                    'transfer_coverage_pct': total_coverage_pct,
                    'recommended_qty': remaining_buy_qty,
                    'rounded_order_qty': remaining_buy_qty,
                    'action_reason': action_reason,
                    'transfer_plan_ids': [(0, 0, vals) for vals in allocations],
                })

    @api.model
    def cron_check_results(self):
        jobs = self.search([], order='id asc')
        if not jobs:
            self._sleep_polling_cron()
            return

        ICP = self.env['ir.config_parameter'].sudo()
        license_key = ICP.get_param('niyu.license_key')
        headers = {'X-License-Key': license_key} if license_key else {}

        for job in jobs:
            try:
                _logger.info("Niyu AI: Checking result for Job %s", job.job_id)

                if job.run_id and job.run_id.state in ('draft', 'queued'):
                    job.run_id.write({'state': 'running'})

                job.state = 'running'
                req = requests.get(f"{API_ENDPOINT}/result/{job.job_id}", headers=headers, timeout=15)

                if req.status_code == 200:
                    resp = req.json()
                    status = resp.get('status')

                    if status == 'done':
                        _logger.info("Niyu AI: Job %s DONE. Processing results...", job.job_id)
                        self._process_results(resp.get('data', []), job.run_id)
                        if job.run_id:
                            job.run_id.write({
                                'state': 'done',
                                'completed_at': fields.Datetime.now(),
                                'message': 'Warehouse-aware forecast results received and written to Odoo.',
                            })
                        job.state = 'done'
                        job.unlink()

                    elif status == 'failed':
                        if job.run_id:
                            job.run_id.write({
                                'state': 'failed',
                                'completed_at': fields.Datetime.now(),
                                'message': resp.get('message', 'Remote job failed.'),
                            })
                        job.state = 'failed'
                        job.unlink()

                    else:
                        _logger.info("Niyu AI: Job %s still processing...", job.job_id)

                else:
                    _logger.warning("Niyu AI: Poll returned %s for job %s", req.status_code, job.job_id)

                if job.exists() and job.start_time and (fields.Datetime.now() - job.start_time).total_seconds() > 1800:
                    _logger.warning("Niyu AI: Job %s timed out. Deleting.", job.job_id)
                    if job.run_id:
                        job.run_id.write({
                            'state': 'failed',
                            'completed_at': fields.Datetime.now(),
                            'message': 'Polling timed out after 30 minutes.',
                        })
                    job.unlink()

            except Exception as e:
                _logger.error("Niyu AI Polling Error for %s: %s", job.job_id, e)

        if not self.search_count([]):
            self._sleep_polling_cron()

    def _process_results(self, data, run):
        ForecastModel = self.env['niyu.forecast.result']
        count = 0
        touched_product_ids = set()
        now = fields.Datetime.now()

        for item in data:
            product = self.env['product.product'].browse(item.get('product_id'))
            if not product.exists():
                continue

            company = run.company_id if run else self.env.company
            product = product.with_company(company)
            touched_product_ids.add(product.id)

            warehouse_id = item.get('warehouse_id')
            warehouse = self.env['stock.warehouse'].browse(warehouse_id) if warehouse_id else self.env['stock.warehouse']
            warehouse = warehouse if warehouse and warehouse.exists() else self.env['stock.warehouse']

            if warehouse:
                self._cleanup_legacy_company_level_lines(company, product)

            existing_record = self._ensure_single_current_line(company, product, warehouse=warehouse or False)

            default_vendor = self._get_default_vendor(product)
            policy = self._select_policy(product, company, warehouse=warehouse or False, vendor=default_vendor)
            vendor = policy.vendor_id if policy and policy.vendor_id else default_vendor

            stock_snapshot = self._get_stock_snapshot(product, company, warehouse=warehouse or False)
            current_stock = stock_snapshot['current_stock']
            reserved_qty = stock_snapshot['reserved_qty']
            incoming_total = stock_snapshot['incoming_total']

            include_drafts = bool(policy.include_draft_rfqs) if policy else False
            include_incoming_po = True if not policy else bool(policy.include_incoming_po)
            include_incoming_transfers = True if not policy else bool(policy.include_incoming_transfers)

            raw_incoming_po_qty = self._compute_incoming_po_qty(
                product,
                company,
                warehouse=warehouse or False,
                vendor=vendor,
                include_drafts=include_drafts,
            )
            incoming_po_qty = raw_incoming_po_qty if include_incoming_po else 0.0
            incoming_transfer_qty = max(0.0, incoming_total - raw_incoming_po_qty) if include_incoming_transfers else 0.0

            forecast_30d = float(item.get('forecast_30d') or item.get('forecast_qty') or 0.0)
            forecast_60d = float(item.get('forecast_60d') or 0.0)
            forecast_90d = float(item.get('forecast_90d') or 0.0)
            forecast_120d = float(item.get('forecast_120d') or 0.0)
            confidence_score = float(item.get('confidence_score') or 0.0)

            chart_data = item.get('chart_data') or ''
            if not isinstance(chart_data, str):
                chart_data = json.dumps(chart_data)

            daily_demand = forecast_30d / 30.0 if forecast_30d else 0.0
            lead_time_days = (
                (policy.lead_time_days if policy and policy.lead_time_days else 0.0)
                or (product.seller_ids[:1].delay if product.seller_ids[:1] else 0.0)
            )
            safety_stock_days = policy.safety_stock_days if policy else 0.0
            safety_stock = daily_demand * safety_stock_days
            reorder_point = (daily_demand * lead_time_days) + safety_stock

            if policy and policy.max_days_of_stock:
                target_stock = daily_demand * policy.max_days_of_stock
            else:
                target_stock = max(reorder_point, forecast_30d)

            net_available = current_stock - reserved_qty + incoming_po_qty + incoming_transfer_qty
            recommended_qty = max(0.0, target_stock - net_available) if (daily_demand > 0 and net_available <= reorder_point) else 0.0
            rounded_order_qty = self._round_qty(recommended_qty, policy)

            manual_ignored = bool(
                existing_record
                and existing_record.ignored
                and (
                    existing_record.ignore_source == 'manual'
                    or (not existing_record.ignore_source and existing_record.action_type == 'ignore')
                )
            )

            action_type = 'ignore' if manual_ignored else ('buy' if rounded_order_qty > 0 else 'watch')
            action_reason = (
                existing_record.action_reason
                if manual_ignored and existing_record.action_reason
                else self._build_action_reason(
                    warehouse=warehouse or False,
                    forecast_30d=forecast_30d,
                    current_stock=current_stock,
                    incoming_po_qty=incoming_po_qty,
                    incoming_transfer_qty=incoming_transfer_qty,
                    reorder_point=reorder_point,
                    target_stock=target_stock,
                    rounded_order_qty=rounded_order_qty,
                )
            )

            vals = {
                'run_id': run.id if run else False,
                'policy_id': policy.id if policy else False,
                'company_id': company.id,
                'warehouse_id': warehouse.id if warehouse else False,
                'product_id': product.id,
                'vendor_id': vendor.id if vendor else False,
                'current_stock': current_stock,
                'reserved_qty': reserved_qty,
                'incoming_po_qty': incoming_po_qty,
                'incoming_transfer_qty': incoming_transfer_qty,
                'forecast_30d': forecast_30d,
                'forecast_60d': forecast_60d,
                'forecast_90d': forecast_90d,
                'forecast_120d': forecast_120d,
                'chart_data': chart_data,
                'confidence_score': confidence_score,
                'lead_time_days': lead_time_days,
                'service_level_target': policy.service_level_target if policy else 95.0,
                'safety_stock': safety_stock,
                'reorder_point': reorder_point,
                'target_stock': target_stock,
                'recommended_qty': recommended_qty,
                'rounded_order_qty': 0.0 if manual_ignored else rounded_order_qty,
                'suggested_transfer_qty': 0.0,
                'transfer_source_warehouse_id': False,
                'transfer_coverage_pct': 0.0,
                'purchase_order_ids': [(6, 0, [])],
                'transfer_picking_ids': [(6, 0, [])],
                'po_last_action_at': False,
                'transfer_last_action_at': False,
                'action_type': action_type,
                'action_reason': action_reason,
                'ignored': manual_ignored,
                'ignore_source': 'manual' if manual_ignored else False,
                'last_updated': now,
            }

            if existing_record:
                existing_record.transfer_plan_ids.unlink()
                existing_record.write(vals)
            else:
                ForecastModel.create(vals)
            count += 1

        if run and touched_product_ids:
            self._apply_transfer_rebalancing(run.company_id, list(touched_product_ids))

        self.env['ir.config_parameter'].sudo().set_param(
            'niyu.last_sync_time',
            fields.Datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )
        self._set_global_status('success', 'Up to date')

        if run:
            run.write({
                'message': f'Updated {count} warehouse-aware replenishment lines with multi-donor planning.',
            })

        _logger.info("Niyu AI: Successfully updated %s warehouse-aware records with multi-donor planning.", count)
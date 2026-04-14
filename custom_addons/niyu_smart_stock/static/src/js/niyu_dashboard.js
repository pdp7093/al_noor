/** @odoo-module */

import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { Component, onWillStart, useState } from "@odoo/owl";

export class NiyuDashboard extends Component {
    static template = "niyu_smart_stock.Dashboard";

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.state = useState({
            loading: true,
            title: "Niyu Planning Overview",
            subtitle: "",
            can_manage: false,
            last_sync: "Never",
            sync_status: "unknown",
            sync_msg: "",
            currency_symbol: "$",
            currency_position: "before",
            latest_run: {
                id: false,
                name: "No runs yet",
                state: false,
                state_label: "",
                message: "",
                warehouse_count: 0,
                quota_syncs_left: false,
            },
            kpis: {
                urgent_count: 0,
                buy_budget: 0,
                buy_line_count: 0,
                rebalance_qty: 0,
                rebalance_count: 0,
                setup_blockers: 0,
            },
            queue_counts: {},
            execution_counts: {},
            health_counts: {},
            setup_counts: {},
            warehouse_pressure: [],
            subscription: {
                tier: "",
                sku_limit: 0,
                manual_limit: 0,
                scheduled_limit: 0,
                max_horizon_days: 0,
                model_type: "",
                seen_at: "",
            },
        });

        onWillStart(async () => {
            await this.fetchData();
        });
    }

    async fetchData() {
        try {
            const result = await this.orm.call("niyu.forecast.result", "get_dashboard_stats", []);
            Object.assign(this.state, result, { loading: false });
        } catch (error) {
            console.error("Niyu Dashboard Fetch Error:", error);
            this.state.loading = false;
            this.state.sync_status = "error";
            this.state.sync_msg = error?.message || "Could not load planning overview.";
        }
    }

    formatCurrency(value) {
        const amount = Number(value || 0);
        const formatted = amount.toLocaleString(undefined, {
            minimumFractionDigits: 0,
            maximumFractionDigits: 0,
        });
        return this.state.currency_position === "after"
            ? `${formatted}${this.state.currency_symbol}`
            : `${this.state.currency_symbol}${formatted}`;
    }

    formatNumber(value, digits = 0) {
        return Number(value || 0).toLocaleString(undefined, {
            minimumFractionDigits: digits,
            maximumFractionDigits: digits,
        });
    }

    formatQuantity(value) {
        const amount = Number(value || 0);
        const digits = Number.isInteger(amount) ? 0 : 2;
        return this.formatNumber(amount, digits);
    }

    getStatusBadgeClass(status) {
        const classes = {
            success: "text-bg-success",
            warning: "text-bg-warning",
            error: "text-bg-danger",
            running: "text-bg-info",
            processing: "text-bg-info",
            unknown: "text-bg-secondary",
        };
        return classes[status] || "text-bg-secondary";
    }

    getStatusLabel(status) {
        const labels = {
            success: "Healthy",
            warning: "Needs Review",
            error: "Attention Needed",
            running: "Running",
            processing: "Running",
            unknown: "Unknown",
        };
        return labels[status] || "Unknown";
    }

    getRunStateBadgeClass(state) {
        const classes = {
            done: "text-bg-success",
            failed: "text-bg-danger",
            queued: "text-bg-info",
            running: "text-bg-info",
            stale: "text-bg-warning",
            expired: "text-bg-warning",
            draft: "text-bg-secondary",
        };
        return classes[state] || "text-bg-secondary";
    }

    async onRunForecastClick() {
        try {
            const action = await this.orm.call("niyu.sync.engine", "action_open_sync_wizard", []);
            if (action) {
                this.action.doAction(action);
            }
        } catch (error) {
            this.action.doAction({
                type: "ir.actions.client",
                tag: "display_notification",
                params: {
                    title: "Run Forecast",
                    message: error?.data?.message || error?.message || "Could not open the sync dialog.",
                    type: "warning",
                    sticky: false,
                },
            });
        }
    }

    openActionLines(domain = [], name = "Action Lines") {
        this.action.doAction({
            type: "ir.actions.act_window",
            name,
            res_model: "niyu.forecast.result",
            view_mode: "list,form",
            views: [[false, "list"], [false, "form"]],
            domain,
            target: "current",
        });
    }

    openLatestRun() {
        if (!this.state.latest_run.id) {
            return;
        }
        this.action.doAction({
            type: "ir.actions.act_window",
            name: this.state.latest_run.name || "Latest Run",
            res_model: "niyu.forecast.run",
            res_id: this.state.latest_run.id,
            views: [[false, "form"]],
            target: "current",
        });
    }

    openUrgentLines() {
        this.openActionLines([
            ["exception_bucket", "in", ["buy_now", "transfer_now", "split_now", "setup"]],
        ], "Urgent Action Lines");
    }

    openBuyBudgetLines() {
        this.openActionLines([
            ["ignored", "=", false],
            ["suggested_buy", ">", 0],
        ], "Lines Needing Purchase");
    }

    openRebalanceLines() {
        this.openActionLines([
            ["ignored", "=", false],
            ["suggested_transfer_qty", ">", 0],
        ], "Lines Needing Rebalance");
    }

    openSetupBlockers() {
        this.openActionLines([
            ["exception_bucket", "=", "setup"],
        ], "Setup Blockers");
    }

    openQueue(bucket) {
        this.openActionLines([
            ["exception_bucket", "=", bucket],
        ], "Action Lines");
    }

    openExecution(status) {
        this.openActionLines([
            ["ignored", "=", false],
            ["execution_status", "=", status],
        ], "Execution Progress");
    }

    openHealth(status) {
        this.openActionLines([
            ["coverage_status", "=", status],
        ], "Inventory Health");
    }

    openSetupIssue(kind) {
        const domain = [["ignored", "=", false]];
        if (kind === "missing_vendor") {
            domain.push(["setup_issue", "=", "missing_vendor"]);
        } else if (kind === "missing_transfer_source") {
            domain.push(["setup_issue", "=", "missing_transfer_source"]);
        } else if (kind === "no_rule") {
            domain.push(["has_policy", "=", false]);
        } else if (kind === "multi_donor") {
            domain.push(["donor_count", ">", 1]);
        }
        this.openActionLines(domain, "Setup & Data Issues");
    }

    openWarehouse(warehouseId, warehouseName) {
        this.openActionLines([
            ["ignored", "=", false],
            ["needs_attention", "=", true],
            ["warehouse_id", "=", warehouseId],
        ], warehouseName || "Warehouse Pressure");
    }
}

registry.category("actions").add("niyu_dashboard_client_action", NiyuDashboard);

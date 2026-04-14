/** @odoo-module */

import { registry } from "@web/core/registry";
import { Component, onWillStart, useRef, useEffect, xml } from "@odoo/owl";
import { loadBundle } from "@web/core/assets";
import { standardFieldProps } from "@web/views/fields/standard_field_props";

export class NiyuChartField extends Component {
    static template = "niyu_smart_stock.NiyuChartField";
    static props = {
        ...standardFieldProps,
    };

    setup() {
        this.chartRef = useRef("chartCanvas");
        this.chartInstance = null;

        onWillStart(async () => {
            // CRITICAL: We must load Chart.js here because the Dashboard doesn't load it anymore.
            await loadBundle("web.chartjs");
        });

        useEffect(() => {
            this.renderChart();
            return () => {
                if (this.chartInstance) {
                    this.chartInstance.destroy();
                }
            };
        });
    }

    renderChart() {
        if (!this.chartRef.el) return;
        
        // Safety: Check if Chart is loaded
        if (typeof Chart === 'undefined') {
            console.error("Niyu AI: Chart.js library not loaded.");
            return;
        }

        const rawData = this.props.record.data[this.props.name];
        if (!rawData) return;

        let dataObj;
        try {
            // Handle both JSON and potential single-quote strings (basic fallback)
            dataObj = JSON.parse(rawData.replace(/'/g, '"'));
        } catch (e) {
            // If simple replace fails, try raw or log error
            try {
                 dataObj = JSON.parse(rawData);
            } catch (e2) {
                 return; // Silent fail if data is garbage
            }
        }

        if (!dataObj || !dataObj.v) return;

        let start = new Date(dataObj.start);
        let values = dataObj.v;
        
        let labels = values.map((_, i) => {
            let d = new Date(start);
            d.setDate(d.getDate() + i);
            return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
        });

        const ctx = this.chartRef.el.getContext('2d');
        
        // Gradient Fill
        const gradient = ctx.createLinearGradient(0, 0, 0, 300);
        gradient.addColorStop(0, 'rgba(37, 99, 235, 0.4)'); 
        gradient.addColorStop(1, 'rgba(37, 99, 235, 0.0)');

        if (this.chartInstance) this.chartInstance.destroy();

        this.chartInstance = new Chart(ctx, {
            type: 'line',
            data: {
                labels: labels,
                datasets: [{
                    label: 'Forecast',
                    data: values,
                    borderColor: '#2563EB',
                    backgroundColor: gradient,
                    borderWidth: 2,
                    fill: true,
                    tension: 0.4,
                    pointRadius: 0,
                    pointHoverRadius: 6
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        mode: 'index',
                        intersect: false,
                    }
                },
                scales: {
                    x: { 
                        display: true,
                        grid: { display: false },
                        ticks: { maxTicksLimit: 6 }
                    },
                    y: { 
                        beginAtZero: true, 
                        grid: { display: false } 
                    }
                }
            }
        });
    }
}

registry.category("fields").add("niyu_chart", {
    component: NiyuChartField,
});
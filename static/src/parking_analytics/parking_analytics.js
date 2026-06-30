/** @odoo-module **/

import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { Component, onWillStart, useState } from "@odoo/owl";
import { AnimatedCounter } from "../parking_shared/animated_counter";
import { LocationPicker } from "../parking_shared/location_picker/location_picker";

export class ParkingAnalytics extends Component {
    static template = "room.ParkingAnalytics";
    static components = { AnimatedCounter, LocationPicker };
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.state = useState({
            data: null,
            heatmap: null,
            forecast: null,
            rangeDays: 7,
            loading: true,
            offices: [],
            selectedOfficeId: null,
        });
        onWillStart(async () => {
            this.state.offices = await this.orm.call("room.room", "get_parking_offices", []);
            await this._reload();
        });
    }

    async _reload() {
        this.state.loading = true;
        const oid = this.state.selectedOfficeId;
        const [data, heatmap, forecast] = await Promise.all([
            this.orm.call("room.booking", "get_parking_analytics_data", [this.state.rangeDays, oid]),
            this.orm.call("room.booking", "get_parking_heatmap_data", [this.state.rangeDays, oid]),
            this.orm.call("room.booking", "get_demand_forecast", [7, oid]),
        ]);
        this.state.data = data;
        this.state.heatmap = heatmap;
        this.state.forecast = forecast;
        this.state.loading = false;
    }

    onLocationChange(ev) {
        const val = ev.target.value;
        this.state.selectedOfficeId = val ? parseInt(val, 10) : null;
        this._reload();
    }

    onLocationPick(officeId) {
        this.state.selectedOfficeId = officeId;
        this._reload();
    }

    // ---------- demand forecast helpers ----------

    get forecastBars() {
        if (!this.state.forecast?.length) return [];
        const capacity = this.state.forecast[0]?.capacity || 1;
        return this.state.forecast.map((d) => {
            const pct = Math.min(100, Math.round((d.predicted / capacity) * 100));
            let color = "#3B82F6";
            if (pct >= 80) color = "#EF4444";
            else if (pct >= 60) color = "#F59E0B";
            return { ...d, pct, color };
        });
    }

    // ---------- heatmap helpers ----------

    /** Return a pastel-to-bold shade for a heatmap cell based on its value vs. max. */
    heatCellBg(value, peak) {
        if (!peak || !value) return "#F1F5F9";
        const ratio = Math.min(1, value / peak);
        // Blend between #DBEAFE (low) → #2563EB (peak).
        const lerp = (a, b) => Math.round(a + (b - a) * ratio);
        const r = lerp(0xDB, 0x25);
        const g = lerp(0xEA, 0x63);
        const b = lerp(0xFE, 0xEB);
        return `rgb(${r},${g},${b})`;
    }

    get heatmapRows() {
        if (!this.state.heatmap) return [];
        const labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
        const peak = this.state.heatmap.max || 0;
        return this.state.heatmap.matrix.map((row, i) => ({
            label: labels[i],
            cells: row.map((v, h) => ({
                value: v,
                bg: this.heatCellBg(v, peak),
                hour: h,
                title: `${labels[i]} ${h.toString().padStart(2, "0")}:00 — ${v} booking${v === 1 ? "" : "s"}`,
            })),
        }));
    }

    get heatmapAxis() {
        // Shown hours: 00, 04, 08, 12, 16, 20 for legibility; blanks in between.
        return Array.from({ length: 24 }, (_, h) =>
            [0, 4, 8, 12, 16, 20].includes(h) ? h.toString().padStart(2, "0") : "",
        );
    }

    onChangeRange(ev) {
        this.state.rangeDays = parseInt(ev.target.value, 10) || 7;
        this._reload();
    }

    // ---------- chart geometry helpers (pure SVG) ----------

    buildAreaChart(labels, values, { width = 620, height = 220, padding = 36, color = "#8B5CF6", fill = "#EDE9FE" } = {}) {
        const max = Math.max(1, ...values);
        const step = labels.length > 1 ? (width - padding * 2) / (labels.length - 1) : 0;
        const points = values.map((v, i) => {
            const x = padding + step * i;
            const y = height - padding - (v / max) * (height - padding * 1.6);
            return { x, y, label: labels[i], value: v };
        });
        const path = points.map((p, i) => `${i === 0 ? "M" : "L"}${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(" ");
        const area = path + ` L${points[points.length - 1].x.toFixed(1)},${height - padding} L${points[0].x.toFixed(1)},${height - padding} Z`;
        const yTicks = 4;
        const gridLines = Array.from({ length: yTicks + 1 }, (_, i) => {
            const v = Math.round((max / yTicks) * (yTicks - i));
            const y = padding * 0.4 + ((height - padding * 1.4) / yTicks) * i;
            return { v, y };
        });
        return { width, height, padding, color, fill, path, area, points, gridLines };
    }

    buildLineChart(labels, values, opts = {}) {
        return this.buildAreaChart(labels, values, { color: "#3B82F6", fill: "#DBEAFE", ...opts });
    }

    buildBarChart(labels, values, { width = 620, height = 260, padding = 40 } = {}) {
        const max = 100;
        const count = labels.length;
        const slot = (width - padding * 2) / count;
        const barW = Math.min(36, slot * 0.55);
        const weekdayMask = [true, true, true, true, true, false, false];
        const bars = values.map((v, i) => {
            const x = padding + slot * i + (slot - barW) / 2;
            const h = (v / max) * (height - padding * 1.6);
            const y = height - padding - h;
            // High weekdays red, mid weekdays blue, weekends pale
            let color = "#3B82F6";
            if (!weekdayMask[i]) color = "#93C5FD";
            else if (v >= 80) color = "#EF4444";
            return { x, y, w: barW, h, v, label: labels[i], color };
        });
        const yTicks = 4;
        const gridLines = Array.from({ length: yTicks + 1 }, (_, i) => {
            const v = (100 / yTicks) * (yTicks - i);
            const y = padding * 0.4 + ((height - padding * 1.4) / yTicks) * i;
            return { v, y };
        });
        return { width, height, padding, bars, gridLines };
    }

    buildDonut(sections, { size = 180, stroke = 26 } = {}) {
        const radius = (size - stroke) / 2;
        const circumference = 2 * Math.PI * radius;
        const total = sections.reduce((s, x) => s + x.count, 0) || 1;
        let offset = 0;
        const arcs = sections.map((sec) => {
            const length = (sec.count / total) * circumference;
            const arc = {
                color: sec.color,
                length,
                gap: circumference - length,
                offset: -offset,  // stroke-dashoffset
                name: sec.name,
                percent: sec.percent,
            };
            offset += length;
            return arc;
        });
        return { size, stroke, radius, circumference, arcs };
    }
}

registry.category("actions").add("room.parking_analytics", ParkingAnalytics);

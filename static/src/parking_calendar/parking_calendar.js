/** @odoo-module **/

import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { Component, onWillStart, useState } from "@odoo/owl";
import { ConfirmationDialog } from "@web/core/confirmation_dialog/confirmation_dialog";
import { useParkingBus, debounced } from "../parking_shared/use_parking_bus";
import { QuickBookDialog } from "../parking_shared/quick_book/quick_book";

const MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
];
const DAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

function localDateStr(date) {
    return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}

export class ParkingCalendar extends Component {
    static template = "room.ParkingCalendar";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.dialog = useService("dialog");
        this.notification = useService("notification");

        const now = new Date();
        this.state = useState({
            loading: true,
            snapshot: null,
            days: {},
            year: now.getFullYear(),
            month: now.getMonth(),   // 0-indexed
        });
        onWillStart(() => this._reload());
        useParkingBus(debounced(() => this._reload(), 200));
    }

    async _reload() {
        this.state.loading = true;
        const result = await this.orm.call("room.booking", "get_my_calendar_month_data", [
            this.state.year,
            this.state.month + 1,   // Python months are 1-indexed
        ]);
        this.state.snapshot = result.snapshot;
        this.state.days = result.days;
        this.state.loading = false;
    }

    // ---------- Navigation ----------
    prevMonth() {
        if (this.state.month === 0) { this.state.year--; this.state.month = 11; }
        else { this.state.month--; }
        this._reload();
    }
    nextMonth() {
        if (this.state.month === 11) { this.state.year++; this.state.month = 0; }
        else { this.state.month++; }
        this._reload();
    }
    goToday() {
        const now = new Date();
        this.state.year = now.getFullYear();
        this.state.month = now.getMonth();
        this._reload();
    }

    get monthLabel() {
        return `${MONTH_NAMES[this.state.month]} ${this.state.year}`;
    }

    get dayLabels() { return DAY_LABELS; }

    // ---------- Calendar grid ----------
    get calendarWeeks() {
        const { year, month } = this.state;
        const firstDay = new Date(year, month, 1);
        const lastDay = new Date(year, month + 1, 0);
        const today = new Date();
        today.setHours(0, 0, 0, 0);

        // Find Monday on or before firstDay
        const startOffset = (firstDay.getDay() + 6) % 7;
        const gridStart = new Date(firstDay);
        gridStart.setDate(1 - startOffset);

        // Find Sunday on or after lastDay
        const endOffset = (7 - ((lastDay.getDay() + 6) % 7 + 1)) % 7;
        const gridEnd = new Date(lastDay);
        gridEnd.setDate(lastDay.getDate() + endOffset);

        const weeks = [];
        const cursor = new Date(gridStart);
        while (cursor <= gridEnd) {
            const week = [];
            for (let d = 0; d < 7; d++) {
                const dateStr = localDateStr(cursor);
                const bookings = this.state.days[dateStr] || [];
                const primary = bookings[0] || null;
                week.push({
                    date: new Date(cursor),
                    dateNum: cursor.getDate(),
                    dateStr,
                    isCurrentMonth: cursor.getMonth() === month,
                    isToday: cursor.getTime() === today.getTime(),
                    isWeekend: cursor.getDay() === 0 || cursor.getDay() === 6,
                    bookings,
                    primary,
                    extra: bookings.length > 1 ? `+${bookings.length - 1}` : "",
                });
                cursor.setDate(cursor.getDate() + 1);
            }
            weeks.push(week);
        }
        return weeks;
    }

    // ---------- Sidebar snapshot rows ----------
    get snapshotRows() {
        const s = this.state.snapshot;
        if (!s) return [];
        return [
            { label: "Confirmed",   count: s.confirmed,  color: "#3B82F6", bg: "#DBEAFE" },
            { label: "Checked In",  count: s.checked_in, color: "#16A34A", bg: "#DCFCE7" },
            { label: "Cancelled",   count: s.cancelled,  color: "#64748B", bg: "#F1F5F9" },
            { label: "No Shows",    count: s.no_show,    color: "#DC2626", bg: "#FEE2E2" },
            { label: "Pending",     count: s.pending,    color: "#D97706", bg: "#FEF3C7" },
        ];
    }

    // ---------- Actions ----------
    onQuickBook() {
        this.dialog.add(QuickBookDialog, { onBooked: () => this._reload() });
    }

    async onDayClick(cell) {
        if (!cell.primary) return;
        await this.action.doAction(
            {
                type: "ir.actions.act_window",
                name: "Parking Booking",
                res_model: "room.booking",
                res_id: cell.primary.id,
                views: [[false, "form"]],
                target: "new",
            },
            { onClose: () => this._reload() },
        );
    }

    onCancelBooking(booking, ev) {
        ev.stopPropagation();
        this.dialog.add(ConfirmationDialog, {
            title: "Cancel booking",
            body: `Cancel booking for ${booking.slot}?`,
            confirmLabel: "Cancel booking",
            cancelLabel: "Keep it",
            confirm: async () => {
                await this.orm.call("room.booking", "action_cancel", [[booking.id]]);
                this.notification.add(`Cancelled — ${booking.slot} is free again.`, { type: "success" });
                await this._reload();
            },
            cancel: () => {},
        });
    }
}

registry.category("actions").add("room.parking_calendar", ParkingCalendar);

/** @odoo-module **/

import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { Component, onWillStart, useState } from "@odoo/owl";
import { AnimatedCounter } from "../parking_shared/animated_counter";
import { useParkingBus, debounced } from "../parking_shared/use_parking_bus";
import { QuickBookDialog } from "../parking_shared/quick_book/quick_book";
import { SmartSuggest } from "../parking_shared/smart_suggest/smart_suggest";

export class ParkingDashboard extends Component {
    static template = "room.ParkingDashboard";
    static components = { AnimatedCounter, SmartSuggest };
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.dialog = useService("dialog");
        this.notification = useService("notification");
        this.state = useState({
            data: null,
            selectedSlot: null,
            loading: true,
            offices: [],
            selectedOfficeId: null,
            livePulse: 0,
        });
        onWillStart(async () => {
            this.state.offices = await this.orm.call("room.room", "get_parking_offices", []);
            await this._reload();
        });
        // 150ms debounce: a bulk op blasts many events; we only want one reload.
        this._busReload = debounced(() => this._liveReload(), 150);
        useParkingBus(this._busReload);
    }

    async _reload() {
        this.state.loading = true;
        this.state.data = await this.orm.call(
            "room.room",
            "get_parking_dashboard_data",
            [this.state.selectedOfficeId]
        );
        this.state.loading = false;
    }

    /** Non-blocking reload used by bus events: no skeleton, just swap data. */
    async _liveReload() {
        try {
            this.state.data = await this.orm.call(
                "room.room",
                "get_parking_dashboard_data",
                [this.state.selectedOfficeId]
            );
            // Keep selectedSlot in sync with its fresh data, or drop it if it's gone.
            if (this.state.selectedSlot) {
                const fresh = this._findSlot(this.state.selectedSlot.id);
                this.state.selectedSlot = fresh || null;
            }
            this.state.livePulse++;
        } catch (_err) {
            // Best-effort; next event will retry.
        }
    }

    _findSlot(slotId) {
        for (const section of this.state.data?.sections || []) {
            for (const s of section.slots) {
                if (s.id === slotId) return s;
            }
        }
        return null;
    }

    onSelectSlot(slot) {
        this.state.selectedSlot = slot;
    }

    async onBookSlot(slot) {
        // When a slot is pre-selected, fall back to the full form (user is
        // signalling they want this specific slot, not "any available one").
        if (slot) {
            await this.action.doAction(
                {
                    type: "ir.actions.act_window",
                    name: `Book ${slot.name}`,
                    res_model: "room.booking",
                    views: [[false, "form"]],
                    target: "new",
                    context: { default_room_id: slot.id },
                },
                { onClose: () => this._reload() },
            );
            return;
        }
        // No slot picked → Quick Book flow (allocator picks the best slot).
        this.dialog.add(QuickBookDialog, {
            onBooked: () => this._reload(),
        });
    }

    async onOpenBookings() {
        await this.action.doAction("room.room_booking_action");
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

    async onJoinWaitlist(slot) {
        await this.action.doAction(
            {
                type: "ir.actions.act_window",
                name: `Join Waitlist — ${slot.name}`,
                res_model: "room.booking.waitlist",
                views: [[false, "form"]],
                target: "new",
                context: {
                    default_room_id: slot.id,
                    default_user_id: this.orm.context?.uid || false,
                },
            },
            {
                onClose: async () => {
                    await this._reload();
                    this.notification.add(
                        `You'll be auto-booked for ${slot.name} the moment it frees up.`,
                        { type: "info" }
                    );
                },
            }
        );
    }

    onRefresh() {
        this._reload();
        this.notification.add("Dashboard refreshed.", { type: "info" });
    }

    get donutStrokeDasharray() {
        const used = this.state.data?.summary?.utilization || 0;
        const circumference = 2 * Math.PI * 42;
        const dash = (used / 100) * circumference;
        return `${dash} ${circumference - dash}`;
    }
}

registry.category("actions").add("room.parking_dashboard", ParkingDashboard);

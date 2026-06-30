/** @odoo-module **/

import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { Component, onWillStart, useState } from "@odoo/owl";
import { ConfirmationDialog } from "@web/core/confirmation_dialog/confirmation_dialog";
import { AnimatedCounter } from "../parking_shared/animated_counter";
import { useParkingBus, debounced } from "../parking_shared/use_parking_bus";
import { QuickBookDialog } from "../parking_shared/quick_book/quick_book";

export class MyBookings extends Component {
    static template = "room.MyBookings";
    static components = { AnimatedCounter };
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.dialog = useService("dialog");
        this.notification = useService("notification");
        this.state = useState({
            data: null,
            waitlist: [],
            activeTab: "upcoming",
            loading: true,
            livePulse: 0,
        });
        onWillStart(() => this._reload());
        // Subscribe to bus events so other people cancelling / admins bulk-
        // acting / waitlist promotions reflect instantly in My Bookings.
        const liveReload = debounced(async () => {
            try {
                const [bookings, waitlist] = await Promise.all([
                    this.orm.call("room.booking", "get_my_bookings_data", []),
                    this.orm.call("room.booking.waitlist", "get_my_waitlist_data", []),
                ]);
                this.state.data = bookings;
                this.state.waitlist = waitlist;
                this.state.livePulse++;
            } catch (_e) { /* next event retries */ }
        }, 200);
        useParkingBus(liveReload);
    }

    async _reload() {
        this.state.loading = true;
        const [bookingsData, waitlistRows] = await Promise.all([
            this.orm.call("room.booking", "get_my_bookings_data", []),
            this.orm.call("room.booking.waitlist", "get_my_waitlist_data", []),
        ]);
        this.state.data = bookingsData;
        this.state.waitlist = waitlistRows;
        this.state.loading = false;
    }

    async onLeaveWaitlist(entry) {
        this.dialog.add(ConfirmationDialog, {
            title: "Leave waitlist",
            body: `Remove your waitlist request for slot ${entry.slot_name} (${entry.date_label}, ${entry.time_range})?`,
            confirmLabel: "Leave waitlist",
            cancelLabel: "Keep my spot",
            confirm: async () => {
                await this.orm.call("room.booking.waitlist", "action_leave_waitlist", [[entry.id]]);
                this.notification.add("Removed from waitlist.", { type: "info" });
                await this._reload();
            },
            cancel: () => {},
        });
    }

    setTab(tab) {
        this.state.activeTab = tab;
    }

    async onCheckIn(booking) {
        const result = await this.orm.call("room.booking", "action_check_in", [[booking.id]]);
        // Manager / admin doing an early check-in: server returns the
        // confirm wizard action instead of completing the check-in.
        if (result && typeof result === "object" && result.type === "ir.actions.act_window") {
            await this.action.doAction(result, { onClose: () => this._reload() });
            return;
        }
        this.notification.add(`Checked in — slot ${booking.slot_name} is now marked in-use.`, {
            type: "success",
        });
        await this._reload();
    }

    async onEdit(bookingId) {
        await this.action.doAction(
            {
                type: "ir.actions.act_window",
                name: "Edit Parking Booking",
                res_model: "room.booking",
                res_id: bookingId,
                views: [[false, "form"]],
                target: "new",
            },
            { onClose: () => this._reload() }
        );
    }

    onCancel(booking) {
        if (booking.is_in_series) {
            this.dialog.add(ConfirmationDialog, {
                title: "Cancel recurring booking",
                body: `This booking is part of a ${booking.recurrence_label.toLowerCase()} series of ${booking.series_size} bookings. What would you like to cancel?`,
                confirmLabel: `Cancel all ${booking.series_size} bookings`,
                cancelLabel: "Just this one",
                confirm: async () => {
                    await this.orm.call("room.booking", "action_cancel_series", [[booking.id]]);
                    this.notification.add(
                        `Cancelled ${booking.series_size} bookings in the series.`,
                        { type: "success" }
                    );
                    await this._reload();
                },
                cancel: async () => {
                    await this.orm.call("room.booking", "action_cancel", [[booking.id]]);
                    this.notification.add(`Booking cancelled — slot ${booking.slot_name} is free again.`, {
                        type: "success",
                    });
                    await this._reload();
                },
            });
            return;
        }
        this.dialog.add(ConfirmationDialog, {
            title: "Cancel booking",
            body: `Cancel booking "${booking.name}" for slot ${booking.slot_name}? The slot will become available again.`,
            confirmLabel: "Cancel booking",
            cancelLabel: "Keep it",
            confirm: async () => {
                await this.orm.call("room.booking", "action_cancel", [[booking.id]]);
                this.notification.add(`Booking cancelled — slot ${booking.slot_name} is free again.`, {
                    type: "success",
                });
                await this._reload();
            },
            cancel: () => {},
        });
    }

    async onNewBooking() {
        await this.action.doAction(
            {
                type: "ir.actions.act_window",
                name: "New Parking Booking",
                res_model: "room.booking",
                views: [[false, "form"]],
                target: "new",
            },
            { onClose: () => this._reload() }
        );
    }

    /** Phase 4: one-shot Quick Book flow (allocator picks slot). */
    onQuickBook() {
        this.dialog.add(QuickBookDialog, {
            onBooked: () => this._reload(),
        });
    }

    get visibleBookings() {
        if (!this.state.data) return [];
        return this.state.activeTab === "upcoming"
            ? this.state.data.upcoming
            : this.state.data.past;
    }
}

registry.category("actions").add("room.my_bookings", MyBookings);

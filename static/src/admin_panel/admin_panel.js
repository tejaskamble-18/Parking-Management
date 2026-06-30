/** @odoo-module **/

import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { Component, onWillStart, useState } from "@odoo/owl";
import { ConfirmationDialog } from "@web/core/confirmation_dialog/confirmation_dialog";
import { AnimatedCounter } from "../parking_shared/animated_counter";
import { useParkingBus, debounced } from "../parking_shared/use_parking_bus";

export class AdminPanel extends Component {
    static template = "room.AdminPanel";
    static components = { AnimatedCounter };
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.dialog = useService("dialog");
        this.notification = useService("notification");
        this.state = useState({
            data: null,
            loading: true,
            activeTab: "slots",
            search: "",
            statusFilter: "all",
            sectionFilter: "all",
            // Phase 4: bulk selection state for slots + bookings tabs.
            selectedSlotIds: {},     // { slotId: true }
            selectedBookingIds: {},  // { bookingId: true }
        });
        onWillStart(() => this._reload());
        // Bus subscription: admin panel is the control tower — it must reflect
        // manager actions (bulk ops, manual overrides) instantly. 200ms
        // debounce so bulk ops don't refresh mid-stream.
        useParkingBus(debounced(() => this._reload(), 200));
    }

    async _reload() {
        this.state.loading = true;
        this.state.data = await this.orm.call(
            "room.room",
            "get_admin_panel_data",
            [],
            {
                status_filter: this.state.statusFilter,
                section_filter: this.state.sectionFilter,
                search: this.state.search,
            }
        );
        this.state.loading = false;
        // Drop stale selections (rows no longer visible).
        const liveSlotIds = new Set((this.state.data?.slots || []).map((s) => s.id));
        for (const id of Object.keys(this.state.selectedSlotIds)) {
            if (!liveSlotIds.has(Number(id))) delete this.state.selectedSlotIds[id];
        }
        const liveBookingIds = new Set((this.state.data?.overrides || []).map((b) => b.id));
        for (const id of Object.keys(this.state.selectedBookingIds)) {
            if (!liveBookingIds.has(Number(id))) delete this.state.selectedBookingIds[id];
        }
    }

    setTab(tab) {
        this.state.activeTab = tab;
    }

    onSearch(ev) {
        this.state.search = ev.target.value;
        this._reload();
    }

    onStatusChange(ev) {
        this.state.statusFilter = ev.target.value;
        this._reload();
    }

    onSectionChange(ev) {
        this.state.sectionFilter = ev.target.value;
        this._reload();
    }

    // ---------- Slot actions ----------

    async onAddSlot() {
        await this.action.doAction(
            {
                type: "ir.actions.act_window",
                name: "New Parking Slot",
                res_model: "room.room",
                views: [[false, "form"]],
                target: "new",
            },
            { onClose: () => this._reload() }
        );
    }

    async onEditSlot(slotId) {
        await this.action.doAction(
            {
                type: "ir.actions.act_window",
                name: "Edit Parking Slot",
                res_model: "room.room",
                res_id: slotId,
                views: [[false, "form"]],
                target: "new",
            },
            { onClose: () => this._reload() }
        );
    }

    async onViewSlotBookings(slot) {
        await this.action.doAction({
            type: "ir.actions.act_window",
            name: `Bookings for ${slot.name}`,
            res_model: "room.booking",
            domain: [["room_id", "=", slot.id]],
            views: [
                [false, "list"],
                [false, "form"],
            ],
            target: "current",
            context: { default_room_id: slot.id },
        });
    }

    async onEditBooking(bookingId) {
        await this.action.doAction(
            {
                type: "ir.actions.act_window",
                name: "Edit Booking",
                res_model: "room.booking",
                res_id: bookingId,
                views: [[false, "form"]],
                target: "new",
            },
            { onClose: () => this._reload() }
        );
    }

    onCancelBooking(booking) {
        this.dialog.add(ConfirmationDialog, {
            title: "Cancel booking",
            body: `Cancel booking "${booking.name}" for slot ${booking.slot_name}?`,
            confirmLabel: "Cancel booking",
            cancelLabel: "Keep it",
            confirm: async () => {
                await this.orm.call("room.booking", "action_cancel", [[booking.id]]);
                this.notification.add(`Cancelled "${booking.name}" — slot ${booking.slot_name} is free.`, {
                    type: "success",
                });
                await this._reload();
            },
            cancel: () => {},
        });
    }

    // ---------- Phase 4: multi-select + bulk actions ----------

    toggleSlotSelect(slotId, ev) {
        // Don't steal clicks from buttons inside the row.
        if (ev?.target?.closest("button")) return;
        if (this.state.selectedSlotIds[slotId]) {
            delete this.state.selectedSlotIds[slotId];
        } else {
            this.state.selectedSlotIds[slotId] = true;
        }
    }

    toggleBookingSelect(bookingId, ev) {
        if (ev?.target?.closest("button")) return;
        if (this.state.selectedBookingIds[bookingId]) {
            delete this.state.selectedBookingIds[bookingId];
        } else {
            this.state.selectedBookingIds[bookingId] = true;
        }
    }

    clearSlotSelection() {
        this.state.selectedSlotIds = {};
    }

    clearBookingSelection() {
        this.state.selectedBookingIds = {};
    }

    get selectedSlotCount() {
        return Object.keys(this.state.selectedSlotIds).length;
    }
    get selectedBookingCount() {
        return Object.keys(this.state.selectedBookingIds).length;
    }

    async onBulkToggleSlots(active) {
        const ids = Object.keys(this.state.selectedSlotIds).map((x) => parseInt(x, 10));
        if (!ids.length) return;
        const label = active ? "Unblock" : "Block";
        this.dialog.add(ConfirmationDialog, {
            title: `${label} ${ids.length} slot${ids.length === 1 ? "" : "s"}?`,
            body: active
                ? `${ids.length} slot(s) will be put back into service.`
                : `${ids.length} slot(s) will be taken offline. Existing bookings are not cancelled, but the slots won't accept new ones.`,
            confirmLabel: label,
            cancelLabel: "Cancel",
            confirm: async () => {
                const n = await this.orm.call("room.room", "admin_bulk_toggle_active", [ids, active]);
                this.notification.add(`${label}ed ${n} slot${n === 1 ? "" : "s"}.`, {
                    type: "success",
                });
                this.clearSlotSelection();
                await this._reload();
            },
            cancel: () => {},
        });
    }

    async onBulkCancelBookings() {
        const ids = Object.keys(this.state.selectedBookingIds).map((x) => parseInt(x, 10));
        if (!ids.length) return;
        this.dialog.add(ConfirmationDialog, {
            title: `Cancel ${ids.length} booking${ids.length === 1 ? "" : "s"}?`,
            body: `This will cancel ${ids.length} booking(s) and free their slots for the waitlist.`,
            confirmLabel: "Cancel bookings",
            cancelLabel: "Keep them",
            confirm: async () => {
                const n = await this.orm.call("room.booking", "admin_bulk_cancel", [ids]);
                this.notification.add(`Cancelled ${n} booking${n === 1 ? "" : "s"}.`, {
                    type: "success",
                });
                this.clearBookingSelection();
                await this._reload();
            },
            cancel: () => {},
        });
    }
}

registry.category("actions").add("room.admin_panel", AdminPanel);

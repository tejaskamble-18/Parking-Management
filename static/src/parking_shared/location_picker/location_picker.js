/** @odoo-module **/
/**
 * Shared location picker used by the Parking Dashboard and Analytics.
 *
 * Renders a polished dropdown with:
 *   - parent/child hierarchy (indented children)
 *   - location-type badge (Head Office / Branch / ...)
 *   - slot count + "N free" pill
 *   - availability status dot (green = has free, red = fully booked)
 *   - search filter
 *   - keyboard & outside-click dismiss
 *
 * Props:
 *   offices      - Array of { id, name, complete_name, parent_id,
 *                              location_type, depth, slot_count, free_count }
 *   selectedId   - number | null
 *   onChange     - (id | null) => void
 *   theme        - "dark" | "light"  (optional, default "light")
 */
import { Component, useState, useRef, onMounted, onWillUnmount, useEffect } from "@odoo/owl";

const LOCATION_TYPE_LABEL = {
    head_office: "HQ",
    branch:      "Branch",
    building:    "Building",
    floor:       "Floor",
    zone:        "Zone",
};

export class LocationPicker extends Component {
    static template = "room.LocationPicker";
    static props = {
        offices:    { type: Array },
        selectedId: { type: [Number, { value: null }], optional: true },
        onChange:   { type: Function },
        theme:      { type: String, optional: true },
    };
    static defaultProps = { theme: "light", selectedId: null };

    setup() {
        this.state = useState({
            open: false,
            search: "",
            panelStyle: "",
        });
        this.rootRef = useRef("root");
        this.triggerRef = useRef("trigger");
        this.panelRef = useRef("panel");

        this._onDocumentClick = (ev) => {
            if (this.rootRef.el && !this.rootRef.el.contains(ev.target)) {
                this.state.open = false;
            }
        };
        this._reposition = () => {
            if (!this.state.open || !this.triggerRef.el) return;
            const rect = this.triggerRef.el.getBoundingClientRect();
            // anchor panel's right edge to trigger's right edge, 6px gap below
            this.state.panelStyle =
                `position: fixed; top: ${rect.bottom + 6}px; ` +
                `right: ${Math.max(12, window.innerWidth - rect.right)}px;`;
        };
        // Close on page scroll (but NOT on scroll inside the panel itself).
        // Capture phase so we see scrolls in any scrollable ancestor.
        this._onScroll = (ev) => {
            if (!this.state.open) return;
            if (this.panelRef.el && this.panelRef.el.contains(ev.target)) return;
            this.state.open = false;
        };
        onMounted(() => {
            document.addEventListener("click", this._onDocumentClick);
            window.addEventListener("resize", this._reposition);
            window.addEventListener("scroll", this._onScroll, true);
        });
        onWillUnmount(() => {
            document.removeEventListener("click", this._onDocumentClick);
            window.removeEventListener("resize", this._reposition);
            window.removeEventListener("scroll", this._onScroll, true);
        });
        // Re-position whenever the panel opens
        useEffect(
            () => { if (this.state.open) this._reposition(); },
            () => [this.state.open]
        );
    }

    get selectedOffice() {
        return this.props.offices.find((o) => o.id === this.props.selectedId);
    }

    get selectedLabel() {
        const o = this.selectedOffice;
        return o ? o.name : "All Locations";
    }

    get selectedMeta() {
        const o = this.selectedOffice;
        if (!o) {
            const total = this.props.offices.reduce((acc, x) =>
                acc + (x.depth === 0 ? x.slot_count : 0), 0);
            const free = this.props.offices.reduce((acc, x) =>
                acc + (x.depth === 0 ? x.free_count : 0), 0);
            return { slot_count: total, free_count: free };
        }
        return { slot_count: o.slot_count, free_count: o.free_count };
    }

    get filteredOffices() {
        const q = this.state.search.trim().toLowerCase();
        if (!q) return this.props.offices;
        return this.props.offices.filter((o) =>
            (o.complete_name || o.name).toLowerCase().includes(q)
        );
    }

    typeLabel(type) {
        return LOCATION_TYPE_LABEL[type] || type;
    }

    toggleOpen() {
        this.state.open = !this.state.open;
        if (this.state.open) {
            this.state.search = "";
        }
    }

    select(id) {
        this.state.open = false;
        this.props.onChange(id);
    }
}

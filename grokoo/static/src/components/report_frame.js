/** @odoo-module **/
// Copyright 2026 CICDoo (https://cicdoo.com)
// SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Grokoo-Commercial
// Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
import { Component, useRef } from "@odoo/owl";

// Renders generated HTML (e.g. a chart/report) inside a sandboxed iframe.
//
// Security: sandbox="allow-same-origin" WITHOUT allow-scripts. The HTML's own
// <script>s never run (it is untrusted markup), while staying same-origin lets
// us read its document to size the frame to its content. NEVER add allow-scripts
// here — allow-scripts + allow-same-origin together let the frame drop its own
// sandbox.
export class ReportFrame extends Component {
    static template = "grokoo.ReportFrame";
    static props = { html: String };

    setup() {
        this.frame = useRef("frame");
    }

    // srcdoc loads asynchronously, so resize on the iframe's load event.
    onLoad() {
        const el = this.frame.el;
        if (!el) {
            return;
        }
        try {
            const doc = el.contentDocument;
            const h = doc && doc.body ? doc.body.scrollHeight : 0;
            if (h) {
                el.style.height = h + "px";
            }
        } catch {
            // Cross-origin read blocked (shouldn't happen with allow-same-origin);
            // the container just scrolls instead.
        }
    }
}

/** @odoo-module **/
// Copyright 2026 CICDoo (https://cicdoo.com)
// SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Grokoo-Commercial
// Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
import { Component, useState } from "@odoo/owl";
import { renderMarkdown } from "../markdown";
import { splitSegments } from "../artifacts";

export class AiMessage extends Component {
    static template = "grokoo.Message";
    static props = {
        message: Object,
        // Called with an HTML string to display it in the artifact panel.
        onOpenArtifact: { type: Function, optional: true },
    };

    setup() {
        // Tool calls are collapsed by default; this toggles the per-message view.
        this.ui = useState({ toolsOpen: false });
    }

    get roleLabel() {
        return { user: "You", assistant: "Assistant", report: "Report", error: "Error" }[
            this.props.message.role
        ] || this.props.message.role;
    }

    get isReport() {
        return this.props.message.role === "report";
    }

    // Ordered render segments for non-user messages: markdown chunks (rendered
    // to safe HTML) interleaved with embedded HTML chunks shown as a card that
    // opens the artifact panel. A report-role message is a single HTML segment.
    // User messages stay plain text and don't use this.
    get segments() {
        const body = this.props.message.body || "";
        if (this.isReport) {
            return [{ type: "html", html: body }];
        }
        const out = [];
        for (const seg of splitSegments(body)) {
            if (!seg.text.trim()) {
                continue;
            }
            out.push(seg.type === "html"
                ? { type: "html", html: seg.text }
                : { type: "md", html: renderMarkdown(seg.text) });
        }
        return out;
    }

    openArtifact(html) {
        this.props.onOpenArtifact?.(html);
    }

    get toolCalls() {
        return this.props.message.tool_calls || [];
    }

    toggleTools() {
        this.ui.toolsOpen = !this.ui.toolsOpen;
    }

    // True while at least one tool call is still running.
    get toolsBusy() {
        return this.toolCalls.some(
            (c) => c.status !== "done" && c.status !== "error");
    }

    get attachments() {
        return this.props.message.attachments || [];
    }

    attachmentIcon(att) {
        const mt = att.mimetype || "";
        if (mt.startsWith("image/")) return "fa-file-image-o";
        if (mt === "application/pdf") return "fa-file-pdf-o";
        return "fa-file-text-o";
    }

    statusIcon(call) {
        if (call.status === "done") return "fa-check text-success";
        if (call.status === "error") return "fa-times text-danger";
        return "fa-spinner fa-spin text-muted";
    }

    summarizeInput(call) {
        try {
            const s = JSON.stringify(call.input || {});
            return s.length > 140 ? s.slice(0, 140) + "…" : s;
        } catch {
            return "";
        }
    }
}

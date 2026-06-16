/** @odoo-module **/
// Copyright 2026 CICDoo (https://cicdoo.com)
// SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Grokoo-Commercial
// Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
import { Component, useState, useRef } from "@odoo/owl";

export class AiComposer extends Component {
    static template = "grokoo.Composer";
    static props = {
        onSend: Function,
        disabled: Boolean,
        sessionId: { type: Number, optional: true },
    };

    setup() {
        // files: [{ id, name, mimetype }] already uploaded and ready to attach.
        this.state = useState({ text: "", files: [], uploading: false, error: "" });
        this.inputRef = useRef("input");
        this.fileRef = useRef("file");
    }

    onKeydown(ev) {
        // Enter sends; Shift+Enter inserts a newline.
        if (ev.key === "Enter" && !ev.shiftKey) {
            ev.preventDefault();
            this._send();
        }
    }

    get canSend() {
        return (
            !this.props.disabled &&
            !this.state.uploading &&
            (!!this.state.text.trim() || this.state.files.length > 0)
        );
    }

    pickFiles() {
        if (this.fileRef.el) {
            this.fileRef.el.click();
        }
    }

    async onFilesChosen(ev) {
        const files = Array.from(ev.target.files || []);
        ev.target.value = ""; // allow re-picking the same file
        if (!files.length || !this.props.sessionId) {
            return;
        }
        this.state.error = "";
        this.state.uploading = true;
        try {
            const form = new FormData();
            form.append("session_id", this.props.sessionId);
            form.append("csrf_token", odoo.csrf_token);
            for (const f of files) {
                form.append("ufile", f);
            }
            const resp = await fetch("/grokoo/upload", {
                method: "POST",
                body: form,
            });
            const data = await resp.json();
            if (!resp.ok || data.error) {
                this.state.error = data.error || "Upload failed.";
                return;
            }
            this.state.files.push(...data.attachments);
        } catch {
            this.state.error = "Upload failed.";
        } finally {
            this.state.uploading = false;
        }
    }

    removeFile(id) {
        this.state.files = this.state.files.filter((f) => f.id !== id);
    }

    _send() {
        const text = this.state.text.trim();
        const files = this.state.files;
        if (!this.canSend) {
            return;
        }
        this.props.onSend(text, files);
        this.state.text = "";
        this.state.files = [];
        this.state.error = "";
        if (this.inputRef.el) {
            this.inputRef.el.style.height = "auto";
        }
    }

    autoGrow(ev) {
        const el = ev.target;
        el.style.height = "auto";
        el.style.height = Math.min(el.scrollHeight, 200) + "px";
    }
}

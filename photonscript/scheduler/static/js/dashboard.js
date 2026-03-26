/**
 * PhotonScript Dashboard — Live WebSocket updates and UI interactions
 */

(function () {
    'use strict';

    const statusEl = document.getElementById('connectionStatus');
    const logEl = document.getElementById('activityLog');
    let ws = null;
    let reconnectDelay = 1000;

    function connect() {
        const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
        ws = new WebSocket(`${protocol}//${location.host}/ws`);

        ws.onopen = function () {
            statusEl.textContent = 'Connected';
            statusEl.className = 'connection-status connected';
            reconnectDelay = 1000;
        };

        ws.onclose = function () {
            statusEl.textContent = 'Disconnected';
            statusEl.className = 'connection-status disconnected';
            setTimeout(connect, reconnectDelay);
            reconnectDelay = Math.min(reconnectDelay * 2, 30000);
        };

        ws.onerror = function () {
            ws.close();
        };

        ws.onmessage = function (event) {
            try {
                const data = JSON.parse(event.data);
                updateDashboard(data);
            } catch (e) {
                console.error('Failed to parse WebSocket message:', e);
            }
        };
    }

    function updateDashboard(data) {
        if (data.telescope) {
            updateTelescopeStatus(data.telescope);
        }
        if (data.projects) {
            updateProjects(data.projects);
        }
    }

    function updateTelescopeStatus(state) {
        const stateEl = document.getElementById('sessionState');
        if (stateEl) {
            stateEl.textContent = (state.session_state || 'idle').toUpperCase();
            stateEl.className = 'value state-' + (state.session_state || 'idle');
        }

        setText('currentTarget', state.current_target || '\u2014');
        setText('currentFilter', state.current_filter || '\u2014');
        setText('imagesTonight', state.images_captured_tonight || 0);

        if (state.guiding) {
            setText('guidingRms', (state.guiding.rms_total_arcsec || 0).toFixed(2) + '"');
        }

        if (state.camera_temp_c !== null && state.camera_temp_c !== undefined) {
            setText('cameraTemp', state.camera_temp_c.toFixed(1) + '\u00B0C');
        }

        // Exposure progress
        const progressDiv = document.getElementById('exposureProgress');
        const fillDiv = document.getElementById('exposureFill');
        if (state.current_exposure_progress > 0) {
            progressDiv.style.display = 'block';
            fillDiv.style.width = (state.current_exposure_progress * 100) + '%';
        } else {
            progressDiv.style.display = 'none';
        }

        // Add activity log entry
        if (state.session_state === 'imaging' && state.current_target) {
            addLogEntry(`Imaging ${state.current_target} [${state.current_filter || '?'}]`);
        }
    }

    function updateProjects(projects) {
        const listEl = document.getElementById('projectList');
        if (!listEl || !projects) return;

        const entries = Object.values(projects);
        if (entries.length === 0) return;

        let html = '';
        for (const project of entries) {
            const target = project.target || {};
            const plans = project.exposure_plans || [];
            const pct = project.completion_pct || 0;

            html += `
                <div class="project-card">
                    <div class="project-header">
                        <span class="project-name">${target.name || 'Unknown'}</span>
                        <span class="project-priority">P${project.priority || 50}</span>
                    </div>
                    <div class="progress-bar">
                        <div class="progress-fill" style="width: ${pct}%"></div>
                    </div>
                    <div class="project-stats">
                        <span>${pct}% complete</span>
                        <span>${project.total_integration_hours || 0}h planned</span>
                    </div>
                    <div class="filter-progress">
                        ${plans.map(ep => `
                            <span class="filter-badge filter-${ep.filter_type}">
                                ${ep.filter_type}: ${ep.acquired || 0}/${ep.count}
                            </span>
                        `).join('')}
                    </div>
                </div>
            `;
        }
        listEl.innerHTML = html;
    }

    function setText(id, value) {
        const el = document.getElementById(id);
        if (el) el.textContent = value;
    }

    let logCount = 0;
    function addLogEntry(message) {
        if (!logEl) return;
        logCount++;
        if (logCount === 1) {
            logEl.innerHTML = '';  // Clear empty state
        }

        const now = new Date();
        const time = now.toLocaleTimeString();

        const entry = document.createElement('div');
        entry.className = 'log-entry';
        entry.innerHTML = `<span class="log-time">${time}</span><span>${message}</span>`;
        logEl.prepend(entry);

        // Keep max 50 entries
        while (logEl.children.length > 50) {
            logEl.removeChild(logEl.lastChild);
        }
    }

    // Initialize
    connect();
})();

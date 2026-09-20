document.addEventListener('DOMContentLoaded', () => {
    fetchJobs();
    pollScraperStatus();

    const modal = document.getElementById('job-modal');
    // Scoped to this modal: there is more than one .close-btn on the page now.
    const closeBtn = document.querySelector('#job-modal .close-btn');

    if (closeBtn) closeBtn.addEventListener('click', closeModal);
    window.addEventListener('click', (e) => {
        if (e.target === modal) closeModal();
        if (e.target === document.getElementById('add-job-modal')) closeAddJob();
        if (e.target === document.getElementById('settings-modal')) closeSettings();
    });
});

window.closeModal = function() {
    document.getElementById('job-modal').classList.remove('active');
    if (window.currentPollInterval) {
        clearInterval(window.currentPollInterval);
    }
}

window.filterColumn = function(input) {
    const filterText = input.value.toLowerCase();
    const column = input.closest('.kanban-column');
    const cards = column.querySelectorAll('.card');
    cards.forEach(card => {
        const textContent = card.textContent.toLowerCase();
        if (textContent.includes(filterText)) {
            card.style.display = 'block';
        } else {
            card.style.display = 'none';
        }
    });
}


let allJobs = [];

async function fetchJobs() {
    try {
        const response = await fetch('/api/jobs');
        allJobs = await response.json();
        renderBoard();
        renderStats();
        
        // Handle deep linking
        const urlParams = new URLSearchParams(window.location.search);
        const jobIdParam = urlParams.get('job_id');
        if (jobIdParam) {
            const job = allJobs.find(j => j.job_id === jobIdParam);
            if (job) {
                openJobDetails(job.job_id);
            }
        }
    } catch (error) {
        console.error('Error fetching jobs:', error);
    }
}

function renderStats() {
    const statsContainer = document.getElementById('stats-container');
    const total = allJobs.length;
    const pending = allJobs.filter(j => j.status === 'new').length;
    const active = allJobs.filter(j => ['to_apply', 'approved', 'applied', 'interviewing'].includes(j.status)).length;
    
    statsContainer.innerHTML = `
        <div class="stat-pill">Total: ${total}</div>
        <div class="stat-pill">Pending AI: ${pending}</div>
        <div class="stat-pill">Active Pipeline: ${active}</div>
    `;
}

let columnLimits = {};

window.loadMore = function(status) {
    columnLimits[status] = (columnLimits[status] || 100) + 100;
    renderBoard();
};

function renderBoard() {
    const columns = {
        'new': document.querySelector('#col-new .kanban-cards'),
        'to_apply': document.querySelector('#col-backlog .kanban-cards'),
        'failed': document.querySelector('#col-failed .kanban-cards'),
        'account_required': document.querySelector('#col-account-required .kanban-cards'),
        'approved': document.querySelector('#col-approved .kanban-cards'),
        'ready_to_submit': document.querySelector('#col-ready-to-submit .kanban-cards'),
        'applied': document.querySelector('#col-applied .kanban-cards'),
        'interviewing': document.querySelector('#col-interviewing .kanban-cards'),
        'rejected': document.querySelector('#col-rejected .kanban-cards')
    };

    // Clear existing cards
    Object.values(columns).forEach(col => {
        if(col) col.innerHTML = '';
    });

    // Group jobs by status
    const groupedJobs = {
        'new': [], 'to_apply': [], 'failed': [], 'account_required': [], 'approved': [], 'ready_to_submit': [], 'applied': [], 'interviewing': [], 'rejected': []
    };

    allJobs.forEach(job => {
        let status = job.status;
        if (['generated', 'synced', 'backlog'].includes(status)) {
            status = 'to_apply';
        }
        if (status === 'scored') {
            status = 'rejected';
        }
        if (groupedJobs[status]) {
            groupedJobs[status].push(job);
        } else {
            groupedJobs['new'].push(job);
        }
    });
    
    // Newest first everywhere. Applied goes by when the application actually
    // went out; every other board by when the job was last touched, so
    // whatever you just moved or annotated is at the top.
    const stamp = (job, status) => status === 'applied'
        ? (job.applied_at || job.updated_at || job.created_at || '')
        : (job.updated_at || job.created_at || '');
    Object.keys(groupedJobs).forEach(status => {
        groupedJobs[status].sort((a, b) => stamp(b, status).localeCompare(stamp(a, status)));
    });

    // Render columns with limits
    Object.entries(columns).forEach(([status, col]) => {
        if (col) {
            const jobsInCol = groupedJobs[status] || [];
            const totalCount = jobsInCol.length;
            const limit = columnLimits[status] || 100;
            
            // Slice the array up to the limit
            const visibleJobs = jobsInCol.slice(0, limit);
            visibleJobs.forEach(job => {
                col.appendChild(createCard(job, status));
            });
            
            // Add Load More button if needed
            if (totalCount > limit) {
                const remaining = totalCount - limit;
                const loadBtn = document.createElement('button');
                loadBtn.className = 'btn btn-primary';
                loadBtn.style.width = '100%';
                loadBtn.style.marginTop = '10px';
                loadBtn.innerText = `Load More (${remaining} remaining)`;
                loadBtn.onclick = () => loadMore(status);
                col.appendChild(loadBtn);
            }

            // Update header count with TOTAL count (not visible count)
            const h2 = col.parentElement.querySelector('h2');
            if (h2) {
                let baseText = h2.getAttribute('data-title');
                if (!baseText) {
                    baseText = h2.innerText.replace(/\s*\(\d+\)$/, '');
                    h2.setAttribute('data-title', baseText);
                }
                h2.innerText = `${baseText} (${totalCount})`;
            }
        }
    });

    setupDragAndDrop();
}


// "2026-09-20 14:38:42.834506" -> "20 Sep 2026, 14:38". The value is written
// by the server in local time, so it is parsed as local, not UTC.
function formatStamp(raw) {
    if (!raw) return '';
    const d = new Date(String(raw).split('.')[0].replace(' ', 'T'));
    if (isNaN(d)) return String(raw).slice(0, 16);
    return d.toLocaleString(undefined, { day: 'numeric', month: 'short', year: 'numeric',
                                         hour: '2-digit', minute: '2-digit' });
}

function createCard(job, columnStatus) {
    const card = document.createElement('div');
    card.className = 'card';
    card.draggable = true;
    card.dataset.id = job.job_id;

    const salaryStr = job.estimated_salary ? job.estimated_salary : 'Unknown';
    const recruiterStr = job.is_recruiter ? '🤝 Recruiter' : (job.is_recruiter === 0 ? '🎯 Direct' : '');

    let metaHtml = '';
    if (job.location) metaHtml += `<span class="meta-tag">📍 ${job.location}</span>`;
    if (salaryStr !== 'Unknown') metaHtml += `<span class="meta-tag">💰 ${salaryStr}</span>`;
    if (recruiterStr) metaHtml += `<span class="meta-tag">${recruiterStr}</span>`;
    // Promoted listings are paid placements, so the evaluator marks them down.
    if (job.is_promoted) metaHtml += `<span class="meta-tag meta-promoted" title="Promoted (paid) listing">📢 Promoted</span>`;
    if (job.writing_assets) metaHtml += `<span class="meta-tag meta-writing">⏳ Writing documents...</span>`;
    // On the Applied board the date that matters is when it went out.
    if (columnStatus === 'applied' && (job.applied_at || job.created_at)) {
        metaHtml += `<span class="meta-tag meta-applied-at" title="Applied on">📮 ${
            formatStamp(job.applied_at || job.created_at)}</span>`;
    }

    card.innerHTML = `
        <div class="card-header">
            <span class="card-company">${job.company} <span style="font-size: 0.7em; color: rgba(255,255,255,0.5); font-weight: normal;">#${job.job_id}</span></span>
            ${job.score ? `<span class="card-score">${job.score}/10</span>` : ''}
        </div>
        <div class="card-title">${job.title}</div>
        <div class="card-meta">
            ${metaHtml}
        </div>
        <div class="card-actions">
            <button class="btn btn-primary" onclick="openJobDetails('${job.job_id}')">View Details</button>
        </div>
    `;

    return card;
}

function openJobDetails(jobId) {
    const job = allJobs.find(j => j.job_id === jobId);
    if (!job) return;

    const modalBody = document.getElementById('modal-body');
    let isProcessing = job.status === 'generating' || job.status === 'evaluating';
    let actionSection = '';
    
    if (isProcessing) {
        actionSection = `<div style="padding: 15px; margin-bottom: 20px; background: rgba(0,150,255,0.1); border-left: 3px solid var(--primary-color); border-radius: 4px;">
            ⏳ <strong>${job.status === 'generating' ? 'Regenerating assets' : 'Re-evaluating job'}...</strong><br>
            <small>This usually takes 20-40 seconds. The data will update automatically.</small>
        </div>`;
    } else {
        // The applier writes its screenshots into the same folder as the CV
        // and cover letter, so split them out: documents to send, versus the
        // record of what was actually sent.
        const allFiles = job.files || [];
        const shots = allFiles.filter(f => f.name.endsWith('.png'));
        const docs = allFiles.filter(f => !f.name.endsWith('.png'));

        let filesHtml = '';
        if (docs.length > 0) {
            const sortedFiles = [...docs].sort((a, b) => {
                const aIsCV = a.name.includes('CV');
                const bIsCV = b.name.includes('CV');
                if (aIsCV && !bIsCV) return -1;
                if (!aIsCV && bIsCV) return 1;
                return a.name.localeCompare(b.name);
            });
            filesHtml = `
                <div class="detail-section">
                    <h4>Generated Assets</h4>
                    <div class="file-list">
                        ${sortedFiles.map(f => {
                            let typeClass = '';
                            if (f.name.endsWith('.docx')) typeClass = 'file-link-docx';
                            else if (f.name.endsWith('.pdf')) typeClass = 'file-link-pdf';
                            return `<a href="${f.url}" class="file-link ${typeClass}" target="_blank">📄 ${f.name}</a>`;
                        }).join('')}
                    </div>
                </div>
            `;
        }

        let traceHtml = '';
        if (shots.length > 0) {
            // Trace shots are named <job>_<NN>_<label>.png, so the number is the
            // running order. Older runs have no number; those sort to the end.
            const stepOf = n => {
                const m = n.match(/_(\d{2})_/);
                return m ? parseInt(m[1], 10) : 999;
            };
            const labelOf = n => {
                const m = n.match(/_\d{2}_(.+)\.png$/);
                return m ? m[1].replace(/_/g, ' ') : n.replace(/\.png$/, '');
            };
            const ordered = [...shots].sort((a, b) => stepOf(a.name) - stepOf(b.name)
                                                   || a.name.localeCompare(b.name));
            traceHtml = `
                <div class="detail-section">
                    <h4>Application Trace (${ordered.length} step${ordered.length === 1 ? '' : 's'})</h4>
                    <div class="trace-grid">
                        ${ordered.map(f => {
                            const step = stepOf(f.name);
                            return `<a href="${f.url}" target="_blank" class="trace-shot" title="${f.name}">
                                <img src="${f.url}" alt="${labelOf(f.name)}" loading="lazy">
                                <span class="trace-caption">${step < 999 ? step + '. ' : ''}${labelOf(f.name)}</span>
                            </a>`;
                        }).join('')}
                    </div>
                </div>
            `;
        }
        
        let contextButtonsHtml = '';
        if (['generated', 'synced', 'backlog', 'to_apply'].includes(job.status)) {
            contextButtonsHtml = `
                <button class="btn btn-primary" style="background-color: #22c55e; border-color: #22c55e;" onclick="changeJobStatus('${job.job_id}', 'approved')">✓ Approve</button>
                <button class="btn btn-primary" onclick="triggerApply('${job.job_id}')">📤 Apply Now</button>
                <button class="btn" onclick="triggerFill('${job.job_id}')">📝 Fill Now</button>
                <button class="btn btn-primary" style="background-color: #ef4444; border-color: #ef4444;" onclick="changeJobStatus('${job.job_id}', 'rejected')">✗ Reject</button>
            `;
        } else if (job.status === 'approved') {
            contextButtonsHtml = `
                <button class="btn btn-primary" onclick="triggerApply('${job.job_id}')">📤 Apply Now</button>
                <button class="btn" onclick="triggerFill('${job.job_id}')">📝 Fill Now</button>
                <button class="btn btn-primary" onclick="showApplyOptions('${job.job_id}')">Mark as Applied</button>
                <button class="btn" style="background-color: #ef4444; border-color: #ef4444; color: white;" onclick="showFailedOptions('${job.job_id}')">Mark as Failed</button>
                <button class="btn" onclick="changeJobStatus('${job.job_id}', 'account_required')">Mark as Account Required</button>
            `;
        } else if (job.status === 'ready_to_submit') {
            contextButtonsHtml = `
                <button class="btn btn-primary" onclick="showApplyOptions('${job.job_id}')">✓ Submitted - Mark as Applied</button>
                <button class="btn btn-primary" onclick="triggerApply('${job.job_id}')">📤 Apply Now (retry)</button>
                <button class="btn" onclick="triggerFill('${job.job_id}')">📝 Fill Now (retry)</button>
                <button class="btn" style="background-color: #ef4444; border-color: #ef4444; color: white;" onclick="showFailedOptions('${job.job_id}')">Mark as Failed</button>
            `;
        } else if (job.status === 'failed') {
            contextButtonsHtml = `
                <button class="btn btn-primary" onclick="triggerApply('${job.job_id}')">📤 Apply Now</button>
                <button class="btn" onclick="triggerFill('${job.job_id}')">📝 Fill Now</button>
                <button class="btn btn-primary" style="background-color: #22c55e; border-color: #22c55e;" onclick="changeJobStatus('${job.job_id}', 'approved')">Return to Approved</button>
                <button class="btn" onclick="changeJobStatus('${job.job_id}', 'account_required')">Move to Account Required</button>
            `;
        } else if (job.status === 'account_required') {
            contextButtonsHtml = `
                <button class="btn btn-primary" onclick="triggerApply('${job.job_id}')">📤 Apply Now</button>
                <button class="btn" onclick="triggerFill('${job.job_id}')">📝 Fill Now</button>
                <button class="btn btn-primary" onclick="showApplyOptions('${job.job_id}')">Mark as Applied</button>
                <button class="btn btn-primary" style="background-color: #22c55e; border-color: #22c55e;" onclick="changeJobStatus('${job.job_id}', 'approved')">Return to Approved</button>
                <button class="btn" style="background-color: #ef4444; border-color: #ef4444; color: white;" onclick="changeJobStatus('${job.job_id}', 'rejected')">✗ Reject</button>
            `;
        } else if (job.status === 'applied') {
            contextButtonsHtml = `
                <button class="btn btn-primary" onclick="changeJobStatus('${job.job_id}', 'interviewing')">Mark as Interviewing</button>
            `;
        }

        actionSection = `
            ${filesHtml}
            ${traceHtml}
            <div class="detail-section">
                ${contextButtonsHtml ? `<div style="display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 15px;">${contextButtonsHtml}</div>` : ''}
                
                <div id="apply-options-${job.job_id}" style="display: none; margin-top: 15px; margin-bottom: 15px;">
                    <textarea id="apply-notes-${job.job_id}" placeholder="Application details/notes..." style="width: 100%; height: 80px; padding: 10px; border-radius: 8px; border: 1px solid rgba(255,255,255,0.1); background: rgba(0,0,0,0.2); color: white; margin-bottom: 10px; font-family: inherit; resize: vertical;"></textarea>
                    <button class="btn btn-primary" onclick="submitApplied('${job.job_id}')">Save & Mark Applied</button>
                </div>
                
                <div id="failed-options-${job.job_id}" style="display: none; margin-top: 15px; margin-bottom: 15px;">
                    <textarea id="failed-notes-${job.job_id}" placeholder="Reason for failure..." style="width: 100%; height: 80px; padding: 10px; border-radius: 8px; border: 1px solid rgba(239,68,68,0.3); background: rgba(0,0,0,0.2); color: white; margin-bottom: 10px; font-family: inherit; resize: vertical;"></textarea>
                    <button class="btn" style="background-color: #ef4444; border-color: #ef4444; color: white;" onclick="submitFailed('${job.job_id}')">Save & Mark Failed</button>
                </div>
                
                <div style="display: flex; gap: 10px; flex-wrap: wrap;">
                    <button class="btn btn-primary" onclick="showRegenOptions('${job.job_id}', 'regenerate')">Regenerate Assets</button>
                    <button class="btn" onclick="showRegenOptions('${job.job_id}', 'reevaluate')">Re-evaluate Job</button>
                </div>
                <div id="regen-options-${job.job_id}" style="display: none; margin-top: 15px;">
                    <textarea id="regen-instructions-${job.job_id}" placeholder="Optional custom instructions for the AI (e.g. 'Emphasize my cloud architecture skills more')..." style="width: 100%; height: 80px; padding: 10px; border-radius: 8px; border: 1px solid rgba(255,255,255,0.1); background: rgba(0,0,0,0.2); color: white; margin-bottom: 10px; font-family: inherit; resize: vertical;"></textarea>
                    <button class="btn btn-primary" onclick="submitRegen('${job.job_id}')" id="regen-submit-btn-${job.job_id}">Start</button>
                    <input type="hidden" id="regen-mode-${job.job_id}" value="">
                </div>
            </div>
        `;
    }

    let notesHtml = '';
    if (job.application_notes) {
        notesHtml = `
            <div class="detail-section">
                <h4>Latest Application Notes</h4>
                <p style="white-space: pre-wrap; font-size: 0.95rem; line-height: 1.5; color: var(--text-primary);">${job.application_notes.replace(/</g, "&lt;").replace(/>/g, "&gt;")}</p>
            </div>
        `;
    }

    let historyHtml = '';
    if (job.history && job.history.length > 0) {
        let historyItems = job.history.map(h => {
            let dateStr = 'Unknown Date';
            if (h.created_at) {
                // Remove the microsecond part if present to ensure proper parsing
                const cleanDate = h.created_at.split('.')[0];
                dateStr = new Date(cleanDate + 'Z').toLocaleString(); 
            }
            if (h.event_type === 'status_change') {
                return `<li><strong>${dateStr}:</strong> Moved to <em>${h.new_status}</em></li>`;
            } else if (h.event_type === 'note_added') {
                return `<li style="margin-top: 5px;"><strong>${dateStr}:</strong> Note saved: <div style="white-space: pre-wrap; padding-left: 10px; border-left: 2px solid rgba(255,255,255,0.1); margin-top: 4px;">${h.note.replace(/</g, "&lt;").replace(/>/g, "&gt;")}</div></li>`;
            }
            return '';
        }).join('');
        
        historyHtml = `
            <div class="detail-section">
                <h4>Timeline</h4>
                <ul style="font-size: 0.9rem; line-height: 1.5; color: var(--text-secondary); padding-left: 20px;">
                    ${historyItems}
                </ul>
            </div>
        `;
    }

    modalBody.innerHTML = `
        <h2>${job.title}</h2>
        <h3>${job.company} • ${job.location}${job.is_promoted ? ' • 📢 Promoted listing' : ''}</h3>

        ${actionSection}
        
        ${notesHtml}
        
        ${historyHtml}
        
        <div class="detail-section">
            <h4>AI Reasoning</h4>
            <p>${job.reasoning || 'No reasoning available.'}</p>
        </div>
        
        <div class="detail-section">
            <h4>Original Listing</h4>
            <a href="${job.link}" target="_blank" class="btn">View on LinkedIn</a>
        </div>
        
        <div class="detail-section">
            <details>
                <summary style="cursor: pointer; color: var(--text-secondary); font-weight: 500; text-transform: uppercase; letter-spacing: 0.05em; font-size: 0.85rem; outline: none;">View Job Description</summary>
                <div style="margin-top: 1rem; white-space: pre-wrap; font-size: 0.9rem; line-height: 1.5; color: var(--text-secondary); max-height: 300px; overflow-y: auto; padding-right: 10px;">
                    ${job.description ? job.description.replace(/</g, "&lt;").replace(/>/g, "&gt;") : 'No description available.'}
                </div>
            </details>
        </div>
    `;

    document.getElementById('job-modal').classList.add('active');
    
    // Polling logic
    if (window.currentPollInterval) clearInterval(window.currentPollInterval);
    if (isProcessing) {
        window.currentPollInterval = setInterval(async () => {
            try {
                const res = await fetch(`/api/jobs/${jobId}`);
                if (res.ok) {
                    const updatedJob = await res.json();
                    if (updatedJob.status !== 'generating' && updatedJob.status !== 'evaluating') {
                        const idx = allJobs.findIndex(j => j.job_id === jobId);
                        if (idx > -1) allJobs[idx] = updatedJob;
                        renderBoard();
                        openJobDetails(jobId);
                    }
                }
            } catch (e) {}
        }, 3000);
    }
}

window.showRegenOptions = function(jobId, mode) {
    document.getElementById(`regen-options-${jobId}`).style.display = 'block';
    document.getElementById(`regen-mode-${jobId}`).value = mode;
    document.getElementById(`regen-submit-btn-${jobId}`).innerText = mode === 'regenerate' ? 'Start Regeneration' : 'Start Re-evaluation';
}

window.submitRegen = async function(jobId) {
    const mode = document.getElementById(`regen-mode-${jobId}`).value;
    const instructions = document.getElementById(`regen-instructions-${jobId}`).value;
    
    try {
        await fetch(`/api/jobs/${jobId}/${mode}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ instructions })
        });
        
        // Update local status to trigger loading UI immediately
        const idx = allJobs.findIndex(j => j.job_id === jobId);
        if (idx > -1) {
            allJobs[idx].status = mode === 'regenerate' ? 'generating' : 'evaluating';
            renderBoard();
            openJobDetails(jobId);
        }
    } catch (e) {
        alert("Failed to start task: " + e);
    }
}

window.manualRefresh = async function(btn) {
    const originalText = btn.innerText;
    btn.innerText = '⏳ Refreshing...';
    btn.disabled = true;
    await fetchJobs();
    
    // Re-apply filters if any are active
    document.querySelectorAll('.column-filter').forEach(input => {
        if (input.value) window.filterColumn(input);
    });

    setTimeout(() => {
        btn.innerText = originalText;
        btn.disabled = false;
    }, 500);
}

window.pullUpdates = async function(btn) {
    const origText = btn.textContent;
    btn.textContent = '⏳ Pulling...';
    btn.disabled = true;
    try {
        const response = await fetch('/api/system/pull', { method: 'POST' });
        const data = await response.json();
        if (data.success) {
            alert('Successfully pulled updates!\n\n' + data.output);
            window.location.reload();
        } else {
            alert('Failed to pull updates:\n\n' + data.error);
        }
    } catch (e) {
        alert('Network error while pulling updates.');
    }
    btn.textContent = origText;
    btn.disabled = false;
};

// Auto-refresh every 30 seconds unless user is interacting
setInterval(() => {
    const isDragging = document.querySelector('.dragging') !== null;
    const isModalOpen = document.getElementById('job-modal').classList.contains('active');
    const isFilterFocused = document.activeElement && document.activeElement.classList.contains('column-filter');
    
    if (!isDragging && !isModalOpen && !isFilterFocused) {
        fetchJobs().then(() => {
            document.querySelectorAll('.column-filter').forEach(input => {
                if (input.value) window.filterColumn(input);
            });
        });
    }
}, 30000);

window.changeJobStatus = async function(jobId, newStatus) {
    try {
        const res = await fetch(`/api/jobs/${jobId}/status`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ status: newStatus })
        });
        const data = await res.json().catch(() => ({}));

        const idx = allJobs.findIndex(j => j.job_id === jobId);
        if (idx > -1) {
            allJobs[idx].status = newStatus;
            // Approving commissions the documents; the card stays in Approved
            // and shows that it is working until the files land.
            allJobs[idx].writing_assets = !!data.generating;
            renderBoard();
            renderStats();
            closeModal();
        }
        if (data.generating) waitForAssets(jobId);
    } catch(e) {
        alert("Failed to update status: " + e);
    }
}

// Poll a freshly approved job until its documents exist (or it lands in
// Failed), then redraw so the badge clears and the files show up.
async function waitForAssets(jobId, attempts = 60) {
    for (let i = 0; i < attempts; i++) {
        await new Promise(r => setTimeout(r, 5000));
        let job;
        try {
            job = await (await fetch(`/api/jobs/${jobId}`)).json();
        } catch (e) {
            continue;
        }
        if ((job.files && job.files.length) || job.status === 'failed') {
            await fetchJobs();
            return;
        }
    }
    const idx = allJobs.findIndex(j => j.job_id === jobId);
    if (idx > -1) { allJobs[idx].writing_assets = false; renderBoard(); }
}

window.showApplyOptions = function(jobId) {
    document.getElementById(`apply-options-${jobId}`).style.display = 'block';
};

window.showFailedOptions = function(jobId) {
    document.getElementById(`failed-options-${jobId}`).style.display = 'block';
};

window.submitApplied = async function(jobId) {
    const notes = document.getElementById(`apply-notes-${jobId}`).value;
    try {
        await fetch(`/api/jobs/${jobId}/status`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ status: 'applied', notes: notes })
        });
        const idx = allJobs.findIndex(j => j.job_id === jobId);
        if (idx > -1) {
            allJobs[idx].status = 'applied';
            allJobs[idx].application_notes = notes;
            renderBoard();
            renderStats();
            closeModal();
        }
    } catch(e) {
        alert("Failed to update status: " + e);
    }
}

window.submitFailed = async function(jobId) {
    const notes = document.getElementById(`failed-notes-${jobId}`).value;
    try {
        await fetch(`/api/jobs/${jobId}/status`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ status: 'failed', notes: notes })
        });
        const idx = allJobs.findIndex(j => j.job_id === jobId);
        if (idx > -1) {
            allJobs[idx].status = 'failed';
            allJobs[idx].application_notes = notes;
            renderBoard();
            renderStats();
            closeModal();
        }
    } catch(e) {
        alert("Failed to update status: " + e);
    }
}

window.triggerScrape = async function(mode = 'full') {
    const btnFull = document.getElementById('run-scraper-btn');
    const btnEval = document.getElementById('run-eval-btn');
    const btnStop = document.getElementById('stop-pipeline-btn');
    const hoverBox = document.getElementById('pipeline-status-hover');
    
    if(btnFull) btnFull.disabled = true;
    if(btnEval) btnEval.disabled = true;
    const btnApplyStart = document.getElementById('run-apply-btn');
    if(btnApplyStart) btnApplyStart.disabled = true;
    if(btnStop) btnStop.style.display = 'inline-block';
    if(btnStop) btnStop.innerText = 'Stop';
    if(btnStop) btnStop.disabled = false;
    if(hoverBox) hoverBox.classList.add('active');
    
    try {
        await fetch('/api/scrape', { 
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ mode: mode })
        });
        pollScraperStatus();
    } catch (e) {
        alert("Failed to start pipeline: " + e);
        if(btnFull) btnFull.disabled = false;
        if(btnEval) btnEval.disabled = false;
        if(btnStop) btnStop.style.display = 'none';
        if(hoverBox) hoverBox.classList.remove('active');
    }
}

// Fill without sending: same run, the applier just stops at the submit
// button. Everything lands in Ready to Submit for a human to send.
window.triggerFill = function(jobId = null) {
    return triggerApply(jobId, false);
}

window.triggerApply = async function(jobId = null, submit = true) {
    const what = jobId ? "this job" : "every approved job";
    const scope = submit ? `Apply for ${what} now?` : `Fill in the form for ${what}, without sending?`;
    const detail = submit
        ? "A browser will open the employer's form, fill it from your profile and SUBMIT it."
            + "\n\nIt will not submit if any required field is empty, any question went "
            + "unanswered, or a field refused input - those land in 'Ready to Submit' for you instead."
        : "A browser will open the employer's form and fill it from your profile, then stop at the "
            + "submit button.\n\nNothing is sent. The job lands in 'Ready to Submit' so you can "
            + "check it and submit yourself.";
    if (!confirm(scope + "\n\n" + detail + "\n\nEvery step is screenshotted.")) return;

    const btnApply = document.getElementById('run-apply-btn');
    const hoverBox = document.getElementById('pipeline-status-hover');
    if (btnApply) btnApply.disabled = true;
    if (hoverBox) hoverBox.classList.add('active');

    try {
        const body = jobId ? { job_ids: [jobId], submit } : { submit };
        const res = await fetch('/api/apply', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });
        const data = await res.json();
        if (data.status === 'already_running') {
            alert("An application run is already in progress.");
            return;
        }
        if (jobId) closeModal();
        pollScraperStatus();
    } catch (e) {
        alert("Failed to start applications: " + e);
        if (btnApply) btnApply.disabled = false;
        if (hoverBox) hoverBox.classList.remove('active');
    }
}

window.stopScrape = async function() {
    const btnStop = document.getElementById('stop-pipeline-btn');
    if(btnStop) {
        btnStop.disabled = true;
        btnStop.innerText = 'Stopping...';
    }
    try {
        await fetch('/api/scrape/stop', { method: 'POST' });
    } catch(e) {
        alert("Failed to request stop: " + e);
        if(btnStop) btnStop.disabled = false;
        if(btnStop) btnStop.innerText = 'Stop';
    }
}


// A single line in the header saying what the pipeline is doing. The hover
// box it replaces was invisible unless you happened to hover the controls.
window.setPipelineBanner = function(text, kind) {
    const el = document.getElementById('pipeline-banner');
    if (!el) return;
    if (!text) { el.style.display = 'none'; el.className = 'pipeline-banner'; return; }
    el.style.display = 'block';
    el.className = 'pipeline-banner' + (kind ? ' pipeline-banner-' + kind : '');
    el.innerText = text;
}

// Shown once when a run finishes: what happened, and for a single job a link
// straight to it.
window.showRunResult = function(result) {
    const el = document.getElementById('run-result');
    if (!el || !result || !result.headline) return;
    let html = `<strong>${result.ok ? '✓' : '⚠️'} ${result.headline}</strong>`;
    const jobs = result.jobs || [];
    if (result.single && jobs.length === 1) {
        html += ` <a href="#" onclick="closeRunResult(); openJobDetails('${jobs[0].job_id}'); return false;">Open the application</a>`;
    } else if (jobs.length) {
        html += '<ul style="margin:0.5rem 0 0 1rem;">'
              + jobs.map(j => `<li>${j.company} - ${j.word}</li>`).join('') + '</ul>';
    }
    html += `<span class="run-result-close" onclick="closeRunResult()">&times;</span>`;
    el.innerHTML = html;
    el.className = 'run-result ' + (result.ok ? 'run-result-ok' : 'run-result-warn');
    el.style.display = 'block';
    fetchJobs();
}

window.closeRunResult = function() {
    const el = document.getElementById('run-result');
    if (el) el.style.display = 'none';
}

window.pollScraperStatus = async function() {
    const btnFull = document.getElementById('run-scraper-btn');
    const btnEval = document.getElementById('run-eval-btn');
    const btnStop = document.getElementById('stop-pipeline-btn');
    const hoverBox = document.getElementById('pipeline-status-hover');
    const stageEl = document.getElementById('pipeline-status-stage');
    const progEl = document.getElementById('pipeline-status-progress');
    const btnApply = document.getElementById('run-apply-btn');

    if (!btnFull) return;
    try {
        const res = await fetch('/api/scrape/status');
        const data = await res.json();
        
        if (data.is_running) {
            btnFull.disabled = true;
            btnEval.disabled = true;
            if (btnApply) btnApply.disabled = true;
            btnStop.style.display = 'inline-block';
            hoverBox.classList.add('active');

            // A run that has finished its work and is only holding filled forms
            // open is not "still working" - saying Stop there reads as a hang.
            if (data.awaiting_review) {
                btnStop.innerText = '✓ Close forms';
                btnStop.classList.add('btn-done');
            } else {
                btnStop.classList.remove('btn-done');
                if (!data.stop_requested) btnStop.innerText = 'Stop';
            }

            if (data.stop_requested) {
                btnStop.disabled = true;
                btnStop.innerText = 'Stopping...';
            }
            
            if (data.current_stage) {
                stageEl.innerText = data.current_stage;
                if (data.total > 0) {
                    progEl.innerText = `${data.processed} / ${data.total} processed`;
                } else {
                    progEl.innerText = `Working...`;
                }
            }
            // The hover box only shows on hover, so mirror the stage into the
            // header where it is visible without going looking for it.
            setPipelineBanner(data.current_stage, data.awaiting_review ? 'done' : 'busy');

            setTimeout(pollScraperStatus, 2000); // Check every 2s
        } else {
            setPipelineBanner('', null);
            if (data.last_result) showRunResult(data.last_result);
            if (btnFull.disabled) {
                // If it was running and now it's not, refresh the board
                btnFull.disabled = false;
                btnEval.disabled = false;
                if (btnApply) btnApply.disabled = false;
                btnStop.style.display = 'none';
                btnStop.disabled = false;
                btnStop.innerText = 'Stop';
                hoverBox.classList.remove('active');
                fetchJobs();
            } else {
                btnFull.disabled = false;
                btnEval.disabled = false;
                if (btnApply) btnApply.disabled = false;
                btnStop.style.display = 'none';
                hoverBox.classList.remove('active');
            }
        }
    } catch(e) {}
}

function setupDragAndDrop() {
    const cards = document.querySelectorAll('.card');
    const columns = document.querySelectorAll('.kanban-cards');

    cards.forEach(card => {
        card.addEventListener('dragstart', () => {
            card.classList.add('dragging');
        });

        card.addEventListener('dragend', () => {
            card.classList.remove('dragging');
        });
    });

    columns.forEach(col => {
        col.addEventListener('dragover', e => {
            e.preventDefault();
            const afterElement = getDragAfterElement(col, e.clientY);
            const draggable = document.querySelector('.dragging');
            if (afterElement == null) {
                col.appendChild(draggable);
            } else {
                col.insertBefore(draggable, afterElement);
            }
        });

        col.addEventListener('drop', async e => {
            const draggable = document.querySelector('.dragging');
            const newStatus = col.parentElement.dataset.status;
            const jobId = draggable.dataset.id;
            
            // Update backend
            try {
                const response = await fetch(`/api/jobs/${jobId}/status`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ status: newStatus })
                });
                if (!response.ok) {
                    console.error('Failed to update status');
                    // In a real app we'd revert the UI
                } else {
                    // Update local state
                    const jobIndex = allJobs.findIndex(j => j.job_id === jobId);
                    if (jobIndex > -1) {
                        allJobs[jobIndex].status = newStatus;
                        renderStats();
                    }
                }
            } catch (err) {
                console.error(err);
            }
        });
    });
}

function getDragAfterElement(container, y) {
    const draggableElements = [...container.querySelectorAll('.card:not(.dragging)')];

    return draggableElements.reduce((closest, child) => {
        const box = child.getBoundingClientRect();
        const offset = y - box.top - box.height / 2;
        if (offset < 0 && offset > closest.offset) {
            return { offset: offset, element: child };
        } else {
            return closest;
        }
    }, { offset: Number.NEGATIVE_INFINITY }).element;
}

// --- Add a job by URL -------------------------------------------------------

window.openAddJob = function() {
    const modal = document.getElementById('add-job-modal');
    const status = document.getElementById('add-job-status');
    if (status) { status.style.display = 'none'; status.innerHTML = ''; }
    const submit = document.getElementById('add-job-submit');
    if (submit) { submit.disabled = false; submit.innerText = 'Fetch & generate'; }
    modal.classList.add('active');
    const input = document.getElementById('add-job-url');
    input.value = '';
    input.focus();
}

window.closeAddJob = function() {
    document.getElementById('add-job-modal').classList.remove('active');
}

function addJobStatus(html, colour) {
    const el = document.getElementById('add-job-status');
    el.style.display = 'block';
    el.style.color = colour || 'var(--text-secondary)';
    el.innerHTML = html;
}

window.submitAddJob = async function() {
    const url = document.getElementById('add-job-url').value.trim();
    const instructions = document.getElementById('add-job-instructions').value.trim();
    if (!url) { addJobStatus('Paste a job URL first.', '#f87171'); return; }

    const submit = document.getElementById('add-job-submit');
    submit.disabled = true;
    submit.innerText = 'Working...';
    addJobStatus('Opening the page...');

    try {
        const res = await fetch('/api/jobs/from-url', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url: url, instructions: instructions })
        });
        const data = await res.json();
        if (data.status === 'already_running') {
            addJobStatus('Another job is being added right now - try again in a moment.', '#fbbf24');
            submit.disabled = false; submit.innerText = 'Fetch & generate';
            return;
        }
        if (data.status === 'error') {
            addJobStatus(data.error, '#f87171');
            submit.disabled = false; submit.innerText = 'Fetch & generate';
            return;
        }
        pollAddJob();
    } catch (e) {
        addJobStatus('Could not start: ' + e, '#f87171');
        submit.disabled = false; submit.innerText = 'Fetch & generate';
    }
}

async function pollAddJob() {
    const submit = document.getElementById('add-job-submit');
    try {
        const res = await fetch('/api/jobs/from-url/status');
        const data = await res.json();

        if (data.running) {
            if (data.stage) addJobStatus(data.stage);
            setTimeout(pollAddJob, 1500);
            return;
        }

        const result = data.result;
        submit.disabled = false;
        submit.innerText = 'Fetch & generate';

        if (!result) {
            addJobStatus('Finished, but no result was reported.', '#fbbf24');
        } else if (!result.ok) {
            addJobStatus(result.error, '#f87171');
        } else {
            const where = result.company_identified ? '' :
                '<br><span style="color:#fbbf24;">The employer could not be identified, so the ' +
                'documents say "Unknown". Add an instruction like "the company is X" and try again.</span>';
            addJobStatus(
                '<strong style="color:#4ade80;">Done.</strong> ' +
                `${result.title || 'Role'} at ${result.company || 'Unknown'}. ` +
                'The CV and cover letter are on the card.' + where +
                `<br><br><button class="btn btn-primary" onclick="closeAddJob(); openJobDetails('${result.job_id}')">` +
                'Open the job</button>', '#4ade80');
            fetchJobs();
        }
    } catch (e) {
        submit.disabled = false;
        submit.innerText = 'Fetch & generate';
        addJobStatus('Lost track of the job: ' + e, '#f87171');
    }
}

// Enter submits from the URL box; Escape closes the dialog.
document.addEventListener('keydown', (e) => {
    const modal = document.getElementById('add-job-modal');
    if (!modal || !modal.classList.contains('active')) return;
    if (e.key === 'Escape') closeAddJob();
    if (e.key === 'Enter' && e.target.id === 'add-job-url') submitAddJob();
});

// --- Search settings --------------------------------------------------------

window.openSettings = function() {
    const modal = document.getElementById('settings-modal');
    const status = document.getElementById('settings-status');
    const btn = document.getElementById('settings-save');
    status.style.display = 'none';
    status.innerHTML = '';
    modal.classList.add('active');
    // Nothing is loaded yet; saving now would post two empty files.
    document.getElementById('settings-criteria').value = '';
    document.getElementById('settings-rules').value = '';
    btn.disabled = true;
    btn.innerText = 'Loading...';

    fetch('/api/settings').then(r => r.json()).then(data => {
        btn.disabled = false;
        btn.innerText = 'Save';
        document.getElementById('settings-criteria').value = data.search_criteria.content;
        document.getElementById('settings-rules').value = data.rules.content;
        document.getElementById('settings-path-criteria').innerText = ' — ' + data.search_criteria.path;
        document.getElementById('settings-path-rules').innerText = ' — ' + data.rules.path;
        document.getElementById('settings-pass').innerText =
            'Jobs scoring ' + data.min_pass_score + '+ go to To Do';
    }).catch(e => {
        status.style.display = 'block';
        status.style.color = '#f87171';
        status.innerText = 'Could not load the settings: ' + e;
    });
}

window.closeSettings = function() {
    document.getElementById('settings-modal').classList.remove('active');
}

window.saveSettings = async function() {
    const btn = document.getElementById('settings-save');
    const status = document.getElementById('settings-status');
    btn.disabled = true;
    btn.innerText = 'Saving...';

    try {
        const res = await fetch('/api/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                search_criteria: document.getElementById('settings-criteria').value,
                rules: document.getElementById('settings-rules').value
            })
        });
        const data = await res.json();
        status.style.display = 'block';

        if (data.error) {
            status.style.color = '#f87171';
            status.innerText = data.error;
        } else {
            const p = data.parsed || {};
            let html = '<strong style="color:#4ade80;">Saved.</strong> ';
            if (p.error) {
                html += '<span style="color:#fbbf24;">But the file did not parse: ' + p.error + '</span>';
            } else {
                html += `The next run will make ${p.searches} searches` +
                    (p.job_types && p.job_types.length ? ` (${p.job_types.join(', ')})` : '') + ':<br>' +
                    p.keywords.map(k =>
                        `&nbsp;&nbsp;<code>${k.keyword}</code> in ${k.locations.join(', ')}`).join('<br>');
            }
            status.style.color = 'var(--text-secondary)';
            status.innerHTML = html;
        }
    } catch (e) {
        status.style.display = 'block';
        status.style.color = '#f87171';
        status.innerText = 'Could not save: ' + e;
    } finally {
        btn.disabled = false;
        btn.innerText = 'Save';
    }
}

document.addEventListener('keydown', (e) => {
    const modal = document.getElementById('settings-modal');
    if (modal && modal.classList.contains('active') && e.key === 'Escape') closeSettings();
});

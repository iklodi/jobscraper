document.addEventListener('DOMContentLoaded', () => {
    fetchJobs();
    pollScraperStatus();

    const modal = document.getElementById('job-modal');
    const closeBtn = document.querySelector('.close-btn');

    closeBtn.addEventListener('click', closeModal);
    window.addEventListener('click', (e) => {
        if (e.target === modal) closeModal();
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
    
    // Render columns with limits
    Object.entries(columns).forEach(([status, col]) => {
        if (col) {
            const jobsInCol = groupedJobs[status] || [];
            const totalCount = jobsInCol.length;
            const limit = columnLimits[status] || 100;
            
            // Slice the array up to the limit
            const visibleJobs = jobsInCol.slice(0, limit);
            visibleJobs.forEach(job => {
                col.appendChild(createCard(job));
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

function createCard(job) {
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
        let filesHtml = '';
        if (job.files && job.files.length > 0) {
            const sortedFiles = [...job.files].sort((a, b) => {
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
        
        let contextButtonsHtml = '';
        if (['generated', 'synced', 'backlog', 'to_apply'].includes(job.status)) {
            contextButtonsHtml = `
                <button class="btn btn-primary" style="background-color: #22c55e; border-color: #22c55e;" onclick="changeJobStatus('${job.job_id}', 'approved')">✓ Approve</button>
                <button class="btn btn-primary" style="background-color: #ef4444; border-color: #ef4444;" onclick="changeJobStatus('${job.job_id}', 'rejected')">✗ Reject</button>
            `;
        } else if (job.status === 'approved') {
            contextButtonsHtml = `
                <button class="btn btn-primary" onclick="triggerApply('${job.job_id}')">📝 Fill Application Now</button>
                <button class="btn btn-primary" onclick="showApplyOptions('${job.job_id}')">Mark as Applied</button>
                <button class="btn" style="background-color: #ef4444; border-color: #ef4444; color: white;" onclick="showFailedOptions('${job.job_id}')">Mark as Failed</button>
                <button class="btn" onclick="changeJobStatus('${job.job_id}', 'account_required')">Mark as Account Required</button>
            `;
        } else if (job.status === 'ready_to_submit') {
            contextButtonsHtml = `
                <button class="btn btn-primary" onclick="showApplyOptions('${job.job_id}')">✓ Submitted - Mark as Applied</button>
                <button class="btn" onclick="triggerApply('${job.job_id}')">Re-fill Form</button>
                <button class="btn" style="background-color: #ef4444; border-color: #ef4444; color: white;" onclick="showFailedOptions('${job.job_id}')">Mark as Failed</button>
            `;
        } else if (job.status === 'failed') {
            contextButtonsHtml = `
                <button class="btn btn-primary" style="background-color: #22c55e; border-color: #22c55e;" onclick="changeJobStatus('${job.job_id}', 'approved')">Return to Approved</button>
                <button class="btn" onclick="changeJobStatus('${job.job_id}', 'account_required')">Move to Account Required</button>
            `;
        } else if (job.status === 'account_required') {
            contextButtonsHtml = `
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
        <h3>${job.company} • ${job.location}</h3>
        
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
        await fetch(`/api/jobs/${jobId}/status`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ status: newStatus })
        });
        
        const idx = allJobs.findIndex(j => j.job_id === jobId);
        if (idx > -1) {
            allJobs[idx].status = newStatus;
            renderBoard();
            renderStats();
            closeModal();
        }
    } catch(e) {
        alert("Failed to update status: " + e);
    }
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

window.triggerApply = async function(jobId = null) {
    const scope = jobId
        ? "Fill in the application form for this job?"
        : "Fill in application forms for the top approved jobs?";
    if (!confirm(scope + "\n\nA browser will open the employer's form and fill it from your profile. "
        + "Nothing is submitted - each job lands in 'Ready to Submit' for you to review.")) return;

    const btnApply = document.getElementById('run-apply-btn');
    const hoverBox = document.getElementById('pipeline-status-hover');
    if (btnApply) btnApply.disabled = true;
    if (hoverBox) hoverBox.classList.add('active');

    try {
        const body = jobId ? { job_ids: [jobId] } : { limit: 5 };
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
            
            setTimeout(pollScraperStatus, 2000); // Check every 2s
        } else {
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

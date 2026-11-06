// Deep links: '#job=<id>' opens that job. Read once on load and on a hash
// change - never on the periodic refresh, which used to reopen the linked job
// every 30 seconds. Older emails link '?job_id=<id>'; that becomes the hash.
function linkedJobId() {
    const m = window.location.hash.match(/^#job=(.+)$/);
    return m ? decodeURIComponent(m[1]) : null;
}

function openLinkedJob() {
    const id = linkedJobId();
    if (id && allJobs.some(j => j.job_id === id)) openJobDetails(id);
}

// Show which job is open in the address bar, replacing - not adding to - the
// history, so Back still leaves the dashboard rather than stepping through jobs.
function setJobHash(jobId) {
    const url = window.location.pathname + (jobId ? '#job=' + encodeURIComponent(jobId) : '');
    if (url !== window.location.pathname + window.location.hash) history.replaceState(null, '', url);
}

document.addEventListener('DOMContentLoaded', () => {
    const legacy = new URLSearchParams(window.location.search).get('job_id');
    if (legacy) history.replaceState(null, '', window.location.pathname + '#job=' + encodeURIComponent(legacy));
    window.addEventListener('hashchange', openLinkedJob);
    Promise.all([loadApplyPolicy(), loadTracks()]).then(fetchJobs).then(openLinkedJob);
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
    setJobHash(null);
    if (window.currentPollInterval) {
        clearInterval(window.currentPollInterval);
    }
}

// A column's filters - the text box, and on To Do a minimum score - apply
// together, to every job in the column, not only the cards drawn so far: the
// board is rebuilt from the jobs that pass (renderBoard), so the count, Load
// More and Select all follow them. Typing waits a moment before redrawing.
let filterTimer;
window.filterColumn = function(control) {
    const column = control.closest('.kanban-column');
    const scoreSelect = column.querySelector('.score-filter');
    if (scoreSelect) {
        try { localStorage.setItem('minScore:' + column.id, scoreSelect.value); } catch (e) {}
    }
    clearTimeout(filterTimer);
    filterTimer = setTimeout(renderBoard, control.tagName === 'SELECT' ? 0 : 200);
}

// Typing, pasting or clearing the box: any change of its text filters.
document.addEventListener('input', e => {
    if (e.target.matches && e.target.matches('input.column-filter')) window.filterColumn(e.target);
});

// The column's own filters, read from its controls: (job) => keep it?
function columnFilter(column) {
    const text = (column?.querySelector('input.column-filter')?.value || '').trim().toLowerCase();
    const scoreSelect = column?.querySelector('.score-filter');
    const minScore = scoreSelect && scoreSelect.value ? Number(scoreSelect.value) : null;
    const active = !!text || minScore !== null;
    const keep = (job, status) => {
        if (minScore !== null && Number(job.score || 0) < minScore) return false;
        if (!text) return true;
        // What the card shows: company, id, score, title and its tags.
        const tags = jobMetaTags(job, status).replace(/<[^>]*>/g, ' ');
        return `${job.company} #${job.job_id} ${job.score || ''}/10 ${job.title} ${tags}`
            .toLowerCase().includes(text);
    };
    return { active, keep };
}

// Kept for callers that redraw after a fetch: filters are part of the redraw now.
function reapplyFilters() {}

// The board-wide track filter: one search track, "Other", or the jobs not
// filed yet. It applies to every column, together with each column's own.
const NO_TRACK = '__none__';
let trackFilter = '';
let configuredTracks = [];
try { trackFilter = localStorage.getItem('trackFilter') || ''; } catch (e) {}

function trackMatches(track) {
    if (!trackFilter) return true;
    return trackFilter === NO_TRACK ? !track : track === trackFilter;
}

// The board is rebuilt: the track decides which jobs are in each column, so
// the column counts, the phone strip and Load More all follow it.
window.setTrackFilter = function(value) {
    trackFilter = value;
    try { localStorage.setItem('trackFilter', value); } catch (e) {}
    renderBoard();
}

async function loadTracks() {
    try { configuredTracks = await (await fetch('/api/tracks')).json(); } catch (e) { configuredTracks = []; }
}

// The filter's options, with how many jobs on the board are in each: the
// configured tracks in their order, then any other name jobs carry.
function renderTrackFilter() {
    const select = document.getElementById('track-filter');
    if (!select) return;
    const counts = {};
    let unfiled = 0;
    allJobs.forEach(job => {
        if (job.track) counts[job.track] = (counts[job.track] || 0) + 1; else unfiled++;
    });
    const names = [...configuredTracks];
    Object.keys(counts).forEach(name => { if (!names.includes(name)) names.push(name); });
    const esc = t => String(t).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
    select.innerHTML = `<option value="">All tracks (${allJobs.length})</option>`
        + names.map(name => `<option value="${esc(name)}">${esc(name)} (${counts[name] || 0})</option>`).join('')
        + `<option value="${NO_TRACK}">Not filed yet (${unfiled})</option>`;
    if (trackFilter && ![...select.options].some(o => o.value === trackFilter)) trackFilter = '';
    select.value = trackFilter;
}

// Restore a remembered minimum score once the page has loaded.
document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.score-filter').forEach(select => {
        try {
            const saved = localStorage.getItem('minScore:' + select.closest('.kanban-column').id);
            if (saved !== null) select.value = saved;
        } catch (e) {}
    });
});


// Jobs from Adzuna's API carry its prefix in their id.
function isAdzuna(job) {
    return String(job.job_id || '').startsWith('adzuna-');
}


// Apply Now honours policies.never_submit_without_review in profile.yaml. When
// submitting is off the button stays visible but disabled, and says why - a
// disabled button swallows hover, so the explanation sits on a wrapper.
window.applyPolicy = { submit_allowed: true, reason: '' };

function applyNowButton(jobId, label = '📤 Apply Now') {
    const btn = `<button class="btn btn-primary" onclick="triggerApply('${jobId}')"`;
    if (window.applyPolicy.submit_allowed) return `${btn}>${label}</button>`;
    const why = window.applyPolicy.reason.replace(/"/g, '&quot;');
    return `<span class="btn-blocked" title="${why}">${btn} disabled>${label}</button></span>`;
}

// Wrap a control so it is disabled and says why - a disabled button swallows
// hover, so the tooltip lives on the wrapper.
function blockControl(el, why) {
    if (!el || el.parentElement.classList.contains('btn-blocked')) return;
    el.disabled = true;
    const wrap = document.createElement('span');
    wrap.className = 'btn-blocked';
    wrap.title = why;
    el.parentNode.insertBefore(wrap, el);
    wrap.appendChild(el);
}

window.modeInfo = { mode: 'full', apply_available: true, add_url_available: true };

function fillNowButton(jobId, label = '📝 Fill Now') {
    const btn = `<button class="btn" onclick="triggerFill('${jobId}')"`;
    if (window.applyPolicy.fill_allowed !== false) return `${btn}>${label}</button>`;
    const why = window.applyPolicy.reason.replace(/"/g, '&quot;');
    return `<span class="btn-blocked" title="${why}">${btn} disabled>${label}</button></span>`;
}

async function loadApplyPolicy() {
    try {
        window.modeInfo = await (await fetch('/api/mode')).json();
    } catch (e) { /* server enforces it regardless */ }
    const badge = document.getElementById('mode-badge');
    if (badge) {
        const safe = window.modeInfo.mode === 'safe';
        badge.textContent = safe ? '🛡️ Safe mode' : '⚠️ Full mode';
        badge.className = 'mode-badge ' + (safe ? 'mode-safe' : 'mode-full');
        badge.title = safe
            ? 'Your logged-in LinkedIn account is never automated. See DISCLAIMER.md.'
            : 'Automates your logged-in LinkedIn account - at your own risk. Set JOBSCRAPER_MODE=safe to avoid it. See DISCLAIMER.md.';
    }
    if (!window.modeInfo.add_url_available) blockControl(document.getElementById('add-job-btn'), window.modeInfo.add_url_reason);
    if (!window.modeInfo.apply_available) {
        // Fill Now needs the same session as Apply Now, so both wait for #15.
        window.applyPolicy = { submit_allowed: false, fill_allowed: false, reason: window.modeInfo.apply_reason };
        blockControl(document.getElementById('run-fill-btn'), window.modeInfo.apply_reason);
    } else try {
        window.applyPolicy = await (await fetch('/api/apply/policy')).json();
        window.applyPolicy.fill_allowed = true;
    } catch (e) { /* keep the permissive default; the server enforces it anyway */ }
    if (!window.applyPolicy.submit_allowed) blockControl(document.getElementById('run-apply-btn'), window.applyPolicy.reason);
}

let allJobs = [];

async function saveApplyUrl(jobId) {
    const input = document.getElementById(`apply-url-${jobId}`);
    const status = document.getElementById(`apply-url-status-${jobId}`);
    try {
        const res = await fetch(`/api/jobs/${encodeURIComponent(jobId)}/apply-url`, {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({url: input.value.trim()}),
        });
        const data = await res.json();
        status.textContent = res.ok ? (data.apply_url ? 'Saved - applying starts from this page.' : 'Cleared.') : data.error;
        status.style.color = res.ok ? '#34d399' : '#f87171';
        if (res.ok) fetchJobs();
    } catch (e) {
        status.textContent = 'Could not save: ' + e;
        status.style.color = '#f87171';
    }
}

async function fetchJobs() {
    try {
        const response = await fetch('/api/jobs');
        allJobs = await response.json();
        renderTrackFilter();
        renderBoard();
        renderStats();
    } catch (error) {
        console.error('Error fetching jobs:', error);
    }
}

function renderStats() {
    const statsContainer = document.getElementById('stats-container');
    const total = allJobs.length;
    const pending = allJobs.filter(j => j.status === 'new').length;
    const active = allJobs.filter(j => ['to_apply', 'approved', 'ready_to_submit', 'easy_apply', 'applied', 'interviewing'].includes(j.status)).length;
    
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
        'applied': document.querySelector('#col-applied .kanban-cards'),
        'interviewing': document.querySelector('#col-interviewing .kanban-cards'),
        'declined': document.querySelector('#col-declined .kanban-cards'),
        'rejected': document.querySelector('#col-rejected .kanban-cards')
    };

    // Clear existing cards
    Object.values(columns).forEach(col => {
        if(col) col.innerHTML = '';
    });

    // Group jobs by status
    const groupedJobs = {
        'new': [], 'to_apply': [], 'failed': [], 'account_required': [], 'approved': [], 'applied': [], 'interviewing': [], 'declined': [], 'rejected': []
    };

    allJobs.forEach(job => {
        if (!trackMatches(job.track)) return;     // the board-wide track filter
        let status = job.status;
        if (['generated', 'synced', 'backlog'].includes(status)) {
            status = 'to_apply';
        }
        if (status === 'scored') {
            status = 'rejected';
        }
        // A filled form (open in Chrome, waiting to be sent) and an Easy Apply
        // job are still approved work waiting on the candidate: they stay in
        // Approved, with a tag, until moved by hand.
        if (status === 'ready_to_submit' || status === 'easy_apply') {
            status = 'approved';
        }
        if (groupedJobs[status]) {
            groupedJobs[status].push(job);
        } else {
            groupedJobs['new'].push(job);
        }
    });
    
    // Every board newest first, by when the job was last touched - a move
    // between columns or a note - so whatever you just handled is on top.
    // Applied cards still *display* the date the application went out, which
    // is the same moment today but does not move when a note is added later.
    const stamp = (job) => job.updated_at || job.created_at || '';
    Object.keys(groupedJobs).forEach(status => {
        groupedJobs[status].sort((a, b) => stamp(b).localeCompare(stamp(a)));
    });

    // Render columns with limits
    Object.entries(columns).forEach(([status, col]) => {
        if (col) {
            const filter = columnFilter(col.closest('.kanban-column'));
            const inTrack = groupedJobs[status] || [];
            const jobsInCol = filter.active ? inTrack.filter(job => filter.keep(job, status)) : inTrack;
            groupedJobs[status] = jobsInCol;          // what the selection may keep
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

            // The header counts every job that passes the filters, drawn or
            // behind Load More; "n of m" while the column's own filter is on.
            const h2 = col.parentElement.querySelector('h2');
            if (h2) {
                let baseText = h2.getAttribute('data-title');
                if (!baseText) {
                    baseText = h2.innerText.replace(/\s*\(\d+( of \d+)?\)$/, '');
                    h2.setAttribute('data-title', baseText);
                }
                h2.dataset.count = totalCount;
                h2.innerText = `${baseText} (${filter.active ? `${totalCount} of ${inTrack.length}` : totalCount})`;
            }
        }
    });

    setupDragAndDrop();
    renderColumnNav();
    Object.keys(columns).forEach(status => {
        const ids = new Set((groupedJobs[status] || []).map(j => j.job_id));
        selectionFor(status).forEach(id => { if (!ids.has(id)) selectionFor(status).delete(id); });
        renderSelectionBar(status);
    });
}


// Phones: a strip of the column names with their counts. Tapping one scrolls
// the board to that column; swiping the board marks the column in view. The
// strip is hidden on wider screens by CSS.
function renderColumnNav() {
    const nav = document.getElementById('column-nav');
    const board = document.getElementById('kanban-board');
    if (!nav || !board) return;
    const columns = [...board.querySelectorAll('.kanban-column')];
    nav.innerHTML = columns.map((col, i) => {
        const h2 = col.querySelector('h2');
        const name = h2 ? (h2.getAttribute('data-title') || h2.innerText) : col.id;
        const count = h2 ? h2.dataset.count : undefined;
        return `<button type="button" data-index="${i}">${name.replace(/\s*\(Backlog\)/i, '')}${count !== undefined ? ` ${count}` : ''}</button>`;
    }).join('');
    nav.querySelectorAll('button').forEach(btn => btn.addEventListener('click', () => {
        const col = columns[Number(btn.dataset.index)];
        board.scrollTo({ left: col.offsetLeft, behavior: 'smooth' });
    }));
    if (!board.dataset.navWatched) {
        board.dataset.navWatched = '1';
        let timer;
        board.addEventListener('scroll', () => {
            clearTimeout(timer);
            timer = setTimeout(markColumnInView, 80);
        }, { passive: true });
    }
    markColumnInView();
}

function markColumnInView() {
    const nav = document.getElementById('column-nav');
    const board = document.getElementById('kanban-board');
    if (!nav || !board || getComputedStyle(nav).display === 'none') return;
    const columns = [...board.querySelectorAll('.kanban-column')];
    const left = board.scrollLeft;
    let best = 0;
    columns.forEach((col, i) => {
        if (Math.abs(col.offsetLeft - left) < Math.abs(columns[best].offsetLeft - left)) best = i;
    });
    nav.querySelectorAll('button').forEach((btn, i) => btn.classList.toggle('active', i === best));
    const active = nav.querySelector('button.active');
    if (active) active.scrollIntoView({ block: 'nearest', inline: 'nearest' });
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

// Where a job's link points, for the button that opens it.
function listingSite(job) {
    if (isAdzuna(job)) return 'Adzuna';
    let host = '';
    try { host = new URL(job.link).hostname; } catch (e) { return 'the listing'; }
    return /(^|\.)linkedin\.com$/i.test(host) ? 'LinkedIn' : host.replace(/^www\./, '');
}

// Where a job came from, for every card, in one word. Adzuna asks for "Jobs by
// Adzuna": it is in the tooltip and the detail view's heading.
function jobSource(job) {
    const src = job.source || '';
    const id = String(job.job_id || '');
    if (isAdzuna(job)) return { label: 'Adzuna', cls: 'meta-adzuna', title: 'Jobs by Adzuna' + (src ? ` - search: ${src.replace(/^adzuna:\s*/, '')}` : '') };
    if (id.startsWith('ats-')) {
        const ats = id.split('-')[1] || '';
        return { label: ats.charAt(0).toUpperCase() + ats.slice(1), cls: 'meta-source', title: "The employer's own job feed" };
    }
    if (src.startsWith('alert')) return { label: 'Alert', cls: 'meta-source', title: src.replace(/^alert:\s*/, 'Job alert: ') };
    if (src === 'link' || id.startsWith('url-')) return { label: 'Link', cls: 'meta-source', title: 'Added with the + button' };
    return { label: 'LinkedIn', cls: 'meta-linkedin', title: 'LinkedIn search' };
}

// The tags a card shows. The detail view shows these too, plus the rest.
function jobMetaTags(job, columnStatus) {
    const salaryStr = job.estimated_salary ? job.estimated_salary : 'Unknown';
    const recruiterStr = job.is_recruiter ? '🤝 Recruiter' : (job.is_recruiter === 0 ? '🎯 Direct' : '');

    let metaHtml = '';
    if (job.location) metaHtml += `<span class="meta-tag">📍 ${job.location}</span>`;
    if (salaryStr !== 'Unknown') metaHtml += `<span class="meta-tag">💰 ${salaryStr}</span>`;
    if (recruiterStr) metaHtml += `<span class="meta-tag">${recruiterStr}</span>`;
    // Promoted listings are paid placements, so the evaluator marks them down.
    if (job.is_promoted) metaHtml += `<span class="meta-tag meta-promoted" title="Promoted (paid) listing">📢 Promoted</span>`;
    if (job.track) metaHtml += `<span class="meta-tag meta-track" title="Search track">🧭 ${String(job.track).replace(/</g, '&lt;')}</span>`;
    // Every card says where the job came from (Adzuna's terms require its label).
    const origin = jobSource(job);
    metaHtml += `<span class="meta-tag ${origin.cls}" title="${origin.title.replace(/"/g, '&quot;')}">${origin.label}</span>`;
    if (job.writing_assets) metaHtml += `<span class="meta-tag meta-writing">⏳ Writing documents...</span>`;
    if (job.status === 'easy_apply') metaHtml += `<span class="meta-tag meta-easy" title="Easy Apply is never automated">✋ Easy Apply - by hand</span>`;
    if (job.status === 'ready_to_submit') metaHtml += `<span class="meta-tag meta-filled" title="The applier filled the form; it is open in Chrome, waiting for you to check and send it">📝 Form filled - send it in Chrome</span>`;
    // On the Applied board the date that matters is when it went out.
    if (columnStatus === 'applied' && (job.applied_at || job.created_at)) {
        metaHtml += `<span class="meta-tag meta-applied-at" title="Applied on">📮 ${
            formatStamp(job.applied_at || job.created_at)}</span>`;
    }
    return metaHtml;
}

// Everything known about a job, for the detail view: the card's tags plus
// what there is no room for on the card.
const STATUS_NAMES = { new: 'New', to_apply: 'To Do', generated: 'To Do', approved: 'Approved',
    ready_to_submit: 'Approved (form filled)', easy_apply: 'Approved (Easy Apply)', applied: 'Applied',
    interviewing: 'Interviewing', failed: 'Failed', account_required: 'Account Required',
    declined: 'Declined by the employer', rejected: 'Discarded', scored: 'Discarded (below the bar)', generating: 'Writing documents',
    evaluating: 'Scoring', applying: 'Applying' };

function jobDetailTags(job) {
    const esc = t => String(t ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;');
    let html = `<span class="meta-tag" title="Job id">#${esc(job.job_id)}</span>`;
    if (job.score) html += `<span class="meta-tag" title="Score">⭐ ${job.score}/10</span>`;
    html += `<span class="meta-tag" title="Column">📋 ${esc(STATUS_NAMES[job.status] || job.status)}</span>`;
    html += jobMetaTags(job, job.status === 'applied' ? 'applied' : '');
    if (job.created_at) html += `<span class="meta-tag" title="Added">🗓 Added ${formatStamp(job.created_at)}</span>`;
    const detail = jobSource(job).title;
    if (detail && !['LinkedIn search', 'Added with the + button', "The employer's own job feed"].includes(detail))
        html += `<span class="meta-tag" title="Search or alert">🔎 ${esc(detail)}</span>`;
    if (job.jd_language) html += `<span class="meta-tag" title="Advert language">🌐 ${esc(job.jd_language)}</span>`;
    if (job.hiring_manager_name) html += `<span class="meta-tag" title="Hiring manager">👤 ${esc(job.hiring_manager_name)}</span>`;
    if (job.apply_type) html += `<span class="meta-tag" title="How to apply">${job.apply_type === 'easy_apply' ? '✋ Easy Apply' : '↗️ Applies on the employer site'}</span>`;
    return html;
}

function createCard(job, columnStatus) {
    const card = document.createElement('div');
    card.className = 'card';
    card.draggable = true;
    card.dataset.id = job.job_id;
    card.dataset.score = job.score || 0;
    card.dataset.track = job.track || '';
    const metaHtml = jobMetaTags(job, columnStatus);

    card.innerHTML = `
        <div class="card-header">
            <input type="checkbox" class="card-select" title="Select for a bulk action"
                   ${isSelected(columnStatus, job.job_id) ? 'checked' : ''}
                   onchange="toggleCardSelection('${columnStatus}', '${job.job_id}', this.checked)">
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
    // The whole card opens the details; the button stays as the obvious target.
    // Controls inside the card keep their own behaviour.
    card.addEventListener('click', (e) => {
        if (e.target.closest('button, a, input, select, textarea, label')) return;
        openJobDetails(job.job_id);
    });

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
                ${applyNowButton(job.job_id)}
                ${fillNowButton(job.job_id)}
                <button class="btn btn-primary" style="background-color: #ef4444; border-color: #ef4444;" onclick="changeJobStatus('${job.job_id}', 'rejected')">✗ Discard</button>
            `;
        } else if (job.status === 'approved') {
            contextButtonsHtml = `
                ${applyNowButton(job.job_id)}
                ${fillNowButton(job.job_id)}
                <button class="btn btn-primary" onclick="showApplyOptions('${job.job_id}')">Mark as Applied</button>
                <button class="btn" style="background-color: #ef4444; border-color: #ef4444; color: white;" onclick="showFailedOptions('${job.job_id}')">Mark as Failed</button>
                <button class="btn" onclick="changeJobStatus('${job.job_id}', 'account_required')">Mark as Account Required</button>
            `;
        } else if (job.status === 'easy_apply') {
            // Not automated, by design: it would run inside LinkedIn on the
            // candidate's own account. So: a link, and the bookkeeping buttons.
            contextButtonsHtml = `
                <a class="btn btn-primary" href="${job.link}" target="_blank" rel="noopener">🔗 Apply on LinkedIn</a>
                <button class="btn btn-primary" onclick="showApplyOptions('${job.job_id}')">✓ Applied - Mark as Applied</button>
                <button class="btn" style="background-color: #ef4444; border-color: #ef4444; color: white;" onclick="showFailedOptions('${job.job_id}')">Mark as Failed</button>
            `;
        } else if (job.status === 'ready_to_submit') {
            contextButtonsHtml = `
                <button class="btn btn-primary" onclick="showApplyOptions('${job.job_id}')">✓ Submitted - Mark as Applied</button>
                ${applyNowButton(job.job_id, '📤 Apply Now (retry)')}
                ${fillNowButton(job.job_id, '📝 Fill Now (retry)')}
                <button class="btn" style="background-color: #ef4444; border-color: #ef4444; color: white;" onclick="showFailedOptions('${job.job_id}')">Mark as Failed</button>
            `;
        } else if (job.status === 'failed') {
            contextButtonsHtml = `
                ${applyNowButton(job.job_id)}
                ${fillNowButton(job.job_id)}
                <button class="btn btn-primary" style="background-color: #22c55e; border-color: #22c55e;" onclick="changeJobStatus('${job.job_id}', 'approved')">Return to Approved</button>
                <button class="btn" onclick="changeJobStatus('${job.job_id}', 'account_required')">Move to Account Required</button>
            `;
        } else if (job.status === 'account_required') {
            contextButtonsHtml = `
                ${applyNowButton(job.job_id)}
                ${fillNowButton(job.job_id)}
                <button class="btn btn-primary" onclick="showApplyOptions('${job.job_id}')">Mark as Applied</button>
                <button class="btn btn-primary" style="background-color: #22c55e; border-color: #22c55e;" onclick="changeJobStatus('${job.job_id}', 'approved')">Return to Approved</button>
                <button class="btn" style="background-color: #ef4444; border-color: #ef4444; color: white;" onclick="changeJobStatus('${job.job_id}', 'rejected')">✗ Discard</button>
            `;
        } else if (job.status === 'applied') {
            contextButtonsHtml = `
                <button class="btn btn-primary" onclick="changeJobStatus('${job.job_id}', 'interviewing')">Mark as Interviewing</button>
                <button class="btn" onclick="showDeclinedOptions('${job.job_id}')">Mark as Declined</button>
            `;
        } else if (job.status === 'interviewing') {
            contextButtonsHtml = `
                <button class="btn" onclick="showDeclinedOptions('${job.job_id}')">Mark as Declined</button>
            `;
        } else if (job.status === 'declined') {
            contextButtonsHtml = `
                <button class="btn" onclick="changeJobStatus('${job.job_id}', 'applied')">Back to Applied</button>
                <button class="btn" onclick="changeJobStatus('${job.job_id}', 'interviewing')">Back to Interviewing</button>
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
                
                <div id="declined-options-${job.job_id}" style="display: none; margin-top: 15px; margin-bottom: 15px;">
                    <textarea id="declined-notes-${job.job_id}" placeholder="Why the employer declined (their reason, or your guess)..." style="width: 100%; height: 80px; padding: 10px; border-radius: 8px; border: 1px solid rgba(185,28,28,0.4); background: rgba(0,0,0,0.2); color: white; margin-bottom: 10px; font-family: inherit; resize: vertical;"></textarea>
                    <button class="btn" style="background-color: #b91c1c; border-color: #b91c1c; color: white;" onclick="submitDeclined('${job.job_id}')">Save & Mark Declined</button>
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
                    <div class="regen-languages" id="regen-languages-${job.job_id}">
                        <label>CV language
                            <select id="regen-cv-lang-${job.job_id}">${languageOptions(`Default (${job.jd_language || 'posting language'})`, job.jd_language)}</select>
                        </label>
                        <label>Cover letter language
                            <select id="regen-cl-lang-${job.job_id}">${languageOptions(`Default (${job.jd_language || 'posting language'})`, job.jd_language)}</select>
                        </label>
                    </div>
                    <textarea id="regen-instructions-${job.job_id}" placeholder="Optional custom instructions for the AI (e.g. 'Emphasize my cloud architecture skills more')..." style="width: 100%; height: 80px; padding: 10px; border-radius: 8px; border: 1px solid rgba(255,255,255,0.1); background: rgba(0,0,0,0.2); color: white; margin-bottom: 10px; font-family: inherit; resize: vertical;"></textarea>
                    <button class="btn btn-primary" onclick="submitRegen('${job.job_id}')" id="regen-submit-btn-${job.job_id}">Start</button>
                    <input type="hidden" id="regen-mode-${job.job_id}" value="">
                </div>
            </div>
        `;
    }

    // Where the applier starts. In safe mode it never opens LinkedIn, so a job
    // that came in with only a LinkedIn link needs the employer's page here.
    let applyLinkHtml = '';
    if (['to_apply', 'generated', 'approved', 'failed', 'account_required', 'ready_to_submit'].includes(job.status)) {
        const onlyLinkedIn = /(^|\.)linkedin\.com$/i.test((() => { try { return new URL(job.link).hostname; } catch (e) { return ''; } })());
        const needed = window.modeInfo.mode === 'safe' && onlyLinkedIn && !job.apply_url;
        const hint = needed
            ? (job.apply_type === 'easy_apply'
                ? 'This posting only takes LinkedIn Easy Apply, which is never automated.'
                : "Needed in safe mode: open the posting on LinkedIn, follow its Apply button, and paste the employer's page here.")
            : "Optional. Fill Now and Apply Now start from this page instead of the posting.";
        const current = (job.apply_url || '').replace(/"/g, '&quot;');
        applyLinkHtml = `
            <div class="detail-section">
                <h4>Employer application link</h4>
                <div style="display: flex; gap: 8px;">
                    <input type="url" id="apply-url-${job.job_id}" value="${current}" placeholder="https://careers.example.com/job/..." style="flex: 1;">
                    <button class="btn" onclick="saveApplyUrl('${job.job_id}')">Save</button>
                </div>
                <p id="apply-url-status-${job.job_id}" style="font-size: 0.85rem; color: ${needed ? '#fbbf24' : 'var(--text-secondary)'}; margin-top: 6px;">${hint}</p>
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
        <h3>${job.company} • ${job.location}${job.is_promoted ? ' • 📢 Promoted listing' : ''}${isAdzuna(job) ? ' • Jobs by Adzuna' : ''}</h3>
        <div class="card-meta" style="margin: 0.6rem 0 1.2rem;">${jobDetailTags(job)}</div>

        ${actionSection}

        ${applyLinkHtml}
        
        ${notesHtml}
        
        ${historyHtml}
        
        <div class="detail-section">
            <h4>AI Reasoning</h4>
            <p>${job.reasoning || 'No reasoning available.'}</p>
        </div>
        
        <div class="detail-section">
            <h4>Original Listing</h4>
            <a href="${job.link}" target="_blank" class="btn">View on ${listingSite(job)}</a>
        </div>
        
        <div class="detail-section">
            <details>
                <summary style="cursor: pointer; color: var(--text-secondary); font-weight: 500; text-transform: uppercase; letter-spacing: 0.05em; font-size: 0.85rem; outline: none;">View Job Description</summary>
                <div style="margin-top: 1rem; white-space: pre-wrap; font-size: 0.9rem; line-height: 1.5; color: var(--text-secondary); max-height: 300px; overflow-y: auto; padding-right: 10px;">
                    ${job.description ? job.description.replace(/</g, "&lt;").replace(/>/g, "&gt;")
                      : (job.status === 'new'
                          ? `Not fetched yet${job.fetch_attempts ? ` (${job.fetch_attempts} failed attempt${job.fetch_attempts > 1 ? 's' : ''})` : ''}: the next nightly run reads it from the listing. The job is scored once it has one.`
                          : 'No description available.')}
                </div>
            </details>
        </div>
    `;

    document.getElementById('job-modal').classList.add('active');
    setJobHash(job.job_id);
    
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

// Regenerate's language dropdowns: English, the home country's languages
// (/api/locale) and the job's own first, then every language alphabetically
// (those again included).
let MAIN_LANGUAGES = ['English'];
fetch('/api/locale').then(r => r.json()).then(d => { MAIN_LANGUAGES = d.main_languages || MAIN_LANGUAGES; })
    .catch(() => {});
const ALL_LANGUAGES = [
    'Arabic', 'Bulgarian', 'Catalan', 'Chinese (Simplified)', 'Croatian', 'Czech', 'Danish',
    'Dutch', 'English', 'Estonian', 'Finnish', 'French', 'German', 'Greek', 'Hebrew', 'Hindi',
    'Hungarian', 'Icelandic', 'Indonesian', 'Italian', 'Japanese', 'Korean', 'Latvian',
    'Lithuanian', 'Luxembourgish', 'Norwegian', 'Polish', 'Portuguese', 'Romanian', 'Romansh',
    'Russian', 'Serbian', 'Slovak', 'Slovenian', 'Spanish', 'Swedish', 'Thai', 'Turkish',
    'Ukrainian', 'Vietnamese'
];

function languageOptions(defaultLabel, jobLanguage) {
    const esc = t => String(t ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
    const opts = names => names.map(n => `<option value="${esc(n)}">${esc(n)}</option>`).join('');
    const own = ALL_LANGUAGES.find(l => l.toLowerCase() === String(jobLanguage || '').trim().toLowerCase());
    const main = own && !MAIN_LANGUAGES.includes(own) ? [...MAIN_LANGUAGES, own] : MAIN_LANGUAGES;
    return `<option value="">${esc(defaultLabel)}</option>`
        + `<optgroup label="Main languages">${opts(main)}</optgroup>`
        + `<optgroup label="All languages">${opts(ALL_LANGUAGES)}</optgroup>`;
}

window.showRegenOptions = function(jobId, mode) {
    document.getElementById(`regen-options-${jobId}`).style.display = 'block';
    // Languages only mean something when documents are written.
    document.getElementById(`regen-languages-${jobId}`).style.display = mode === 'regenerate' ? '' : 'none';
    document.getElementById(`regen-mode-${jobId}`).value = mode;
    document.getElementById(`regen-submit-btn-${jobId}`).innerText = mode === 'regenerate' ? 'Start Regeneration' : 'Start Re-evaluation';
}

window.submitRegen = async function(jobId) {
    const mode = document.getElementById(`regen-mode-${jobId}`).value;
    const instructions = document.getElementById(`regen-instructions-${jobId}`).value;
    const body = { instructions };
    if (mode === 'regenerate') {
        body.cv_language = document.getElementById(`regen-cv-lang-${jobId}`).value;
        body.cl_language = document.getElementById(`regen-cl-lang-${jobId}`).value;
    }
    
    try {
        await fetch(`/api/jobs/${jobId}/${mode}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
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
    
    reapplyFilters();

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
        fetchJobs().then(reapplyFilters);
    }
}, 30000);

// Empty the To Do column: every job in it moves to Discarded, each with its
// history entry, after a confirmation that says how many.
window.clearBacklog = async function() {
    const count = allJobs.filter(j => ['to_apply', 'generated'].includes(j.status)).length;
    if (!count) { alert('To Do is already empty.'); return; }
    if (!confirm(`Move all ${count} jobs in To Do to Discarded?${trackFilter ? ' That is every track, not only the one selected.' : ''}\n\nThey stay in the database and can be moved back one by one.`)) return;
    try {
        const res = await fetch('/api/backlog/clear', { method: 'POST' });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || res.statusText);
        await fetchJobs();
    } catch (e) {
        alert('Could not clear the backlog: ' + e.message);
    }
}

// Empty the Failed column the same way.
window.clearFailed = async function() {
    const count = allJobs.filter(j => j.status === 'failed').length;
    if (!count) { alert('Failed is already empty.'); return; }
    if (!confirm(`Move all ${count} jobs in Failed to Discarded?${trackFilter ? ' That is every track, not only the one selected.' : ''}\n\nThey stay in the database and can be moved back one by one.`)) return;
    try {
        const res = await fetch('/api/failed/clear', { method: 'POST' });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || res.statusText);
        await fetchJobs();
    } catch (e) {
        alert('Could not clear Failed: ' + e.message);
    }
}

// Selecting cards for a bulk action. Each column keeps its own selection,
// by job id, so it survives the board being redrawn every 30 seconds.
const selections = {};
function selectionFor(status) {
    return selections[status] || (selections[status] = new Set());
}
function isSelected(status, jobId) {
    return selectionFor(status).has(jobId);
}

// What the menu offers for the cards selected in each column - the same moves
// the detail view offers there. `status` moves them; `run` starts something.
const FILL = { label: '📝 Fill Now', run: 'fill' };
const APPLY = { label: '📤 Apply Now', run: 'apply' };
const REEVALUATE = { label: 'Re-evaluate', run: 'reevaluate' };
const REJECT = { label: '✗ Discard', status: 'rejected' };
const DECLINED = { label: 'Mark as Declined', status: 'declined' };
const BULK_ACTIONS = {
    new: [REEVALUATE, REJECT],
    to_apply: [{ label: '✓ Approve', status: 'approved' }, FILL, APPLY, REEVALUATE, REJECT],
    failed: [{ label: 'Return to Approved', status: 'approved' }, FILL, APPLY,
             { label: 'Move to Account Required', status: 'account_required' }, REJECT],
    account_required: [{ label: 'Return to Approved', status: 'approved' }, FILL, APPLY,
                       { label: 'Mark as Applied', status: 'applied' }, REJECT],
    approved: [FILL, APPLY, { label: 'Mark as Applied', status: 'applied' },
               { label: 'Mark as Failed', status: 'failed' },
               { label: 'Mark as Account Required', status: 'account_required' }, REJECT],
    applied: [{ label: 'Mark as Interviewing', status: 'interviewing' }, DECLINED],
    interviewing: [{ label: 'Back to Applied', status: 'applied' }, DECLINED],
    declined: [{ label: 'Back to Applied', status: 'applied' }, { label: 'Back to Interviewing', status: 'interviewing' }],
    rejected: [{ label: 'Return to To Do', status: 'to_apply' }, REEVALUATE],
};

function columnFor(status) {
    return document.querySelector(`.kanban-column[data-status="${status}"]`);
}

// The checkbox and menu above a column's cards, created the first time.
function selectionBar(status) {
    const column = columnFor(status);
    if (!column) return null;
    let bar = column.querySelector('.selection-bar');
    if (!bar) {
        bar = document.createElement('div');
        bar.className = 'selection-bar';
        bar.innerHTML = `
            <label class="select-all"><input type="checkbox" onchange="selectAllVisible('${status}', this.checked)"> Select all</label>
            <select class="bulk-actions" onchange="runBulkAction('${status}', this)"></select>`;
        column.insertBefore(bar, column.querySelector('.kanban-cards'));
    }
    return bar;
}

function visibleCards(status) {
    const column = columnFor(status);
    return column ? [...column.querySelectorAll('.kanban-cards .card')].filter(c => c.style.display !== 'none') : [];
}

// Bring the bar in line with the selection: the "Select all" box's state, and
// the menu, which shows only while something is selected.
function renderSelectionBar(status) {
    const bar = selectionBar(status);
    if (!bar) return;
    const selected = selectionFor(status);
    const visible = visibleCards(status);
    const all = bar.querySelector('.select-all input');
    all.checked = visible.length > 0 && visible.every(c => selected.has(c.dataset.id));
    all.indeterminate = !all.checked && selected.size > 0;
    all.disabled = visible.length === 0;
    const menu = bar.querySelector('.bulk-actions');
    menu.style.display = selected.size ? '' : 'none';
    if (!selected.size) return;
    const actions = (BULK_ACTIONS[status] || []).map((action, i) => {
        let blocked = '';
        if (action.run === 'apply' && !window.applyPolicy.submit_allowed) blocked = window.applyPolicy.reason;
        if (action.run === 'fill' && window.applyPolicy.fill_allowed === false) blocked = window.applyPolicy.reason;
        return `<option value="${i}" ${blocked ? 'disabled' : ''} title="${(blocked || '').replace(/"/g, '&quot;')}">${action.label}</option>`;
    });
    menu.innerHTML = `<option value="">${selected.size} selected - choose an action…</option>` + actions.join('')
        + `<option value="clear">Clear selection</option>`;
}

// Tick the card boxes to match the selection, then redraw the bar.
function syncSelection(status) {
    const column = columnFor(status);
    if (!column) return;
    const selected = selectionFor(status);
    column.querySelectorAll('.card').forEach(card => {
        const box = card.querySelector('.card-select');
        if (box) box.checked = selected.has(card.dataset.id);
        card.classList.toggle('selected', selected.has(card.dataset.id));
    });
    renderSelectionBar(status);
}

window.toggleCardSelection = function(status, jobId, checked) {
    if (checked) selectionFor(status).add(jobId); else selectionFor(status).delete(jobId);
    syncSelection(status);
}

// "Select all" takes only the cards on screen: not those a filter hides, nor
// those behind Load More.
window.selectAllVisible = function(status, checked) {
    const selected = selectionFor(status);
    if (checked) visibleCards(status).forEach(c => selected.add(c.dataset.id));
    else selected.clear();
    syncSelection(status);
}

window.runBulkAction = async function(status, menu) {
    const choice = menu.value;
    menu.value = '';
    if (!choice) return;
    const selected = selectionFor(status);
    if (choice === 'clear') { selected.clear(); syncSelection(status); return; }
    const action = (BULK_ACTIONS[status] || [])[Number(choice)];
    const ids = [...selected];
    if (!action || !ids.length) return;
    const n = ids.length === 1 ? '1 job' : `${ids.length} jobs`;

    if (action.run === 'fill' || action.run === 'apply') {
        // triggerApply asks for its own confirmation.
        if (await triggerApply(ids, action.run === 'apply')) { selected.clear(); syncSelection(status); }
        return;
    }
    let note = null;
    if (action.status === 'declined') {
        note = prompt(`Why did the employer decline ${n === '1 job' ? 'it' : 'them'}? (Saved on each job; leave empty for none.)`);
        if (note === null) return;                 // Cancel
        note = note.trim() || null;
    }
    const question = action.run === 'reevaluate'
        ? `Re-evaluate ${n}? They are scored again one after another; jobs you approved or applied to stay where they are.`
        : `${action.label.replace(/^[^A-Za-z]+/, '')}: ${n}?`
          + (action.status === 'approved' ? '\n\nDocuments are written for each one that has none, one after another.' : '');
    // Declining already asked for the reason; that answer is the confirmation.
    if (action.status !== 'declined' && !confirm(question)) return;
    try {
        const res = await fetch('/api/jobs/bulk', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(action.run === 'reevaluate'
                ? { job_ids: ids, action: 'reevaluate' }
                : { job_ids: ids, action: 'status', status: action.status, notes: note })
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || res.statusText);
        selected.clear();
        await fetchJobs();
        if (data.skipped) alert(`${data.skipped} of them were skipped: a background run is working on them.`);
        (data.generating || []).forEach(id => {
            const job = allJobs.find(j => j.job_id === id);
            if (job) job.writing_assets = true;
        });
        if ((data.generating || []).length) {
            renderBoard();
            // Written one after another, so the later ones get longer to wait.
            data.generating.forEach((id, i) => waitForAssets(id, 60 * (i + 1)));
        }
    } catch (e) {
        alert('Could not run the action: ' + e.message);
    }
}

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

window.showDeclinedOptions = function(jobId) {
    document.getElementById(`declined-options-${jobId}`).style.display = 'block';
    document.getElementById(`declined-notes-${jobId}`).focus();
};

// Like Mark as Failed: the reason goes into the job's notes and its history.
window.submitDeclined = async function(jobId) {
    const notes = document.getElementById(`declined-notes-${jobId}`).value.trim();
    if (!notes && !confirm('Mark as declined without a reason?')) return;
    try {
        const res = await fetch(`/api/jobs/${jobId}/status`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(notes ? { status: 'declined', notes } : { status: 'declined' })
        });
        if (!res.ok) throw new Error(res.statusText);
        const idx = allJobs.findIndex(j => j.job_id === jobId);
        if (idx > -1) {
            allJobs[idx].status = 'declined';
            if (notes) allJobs[idx].application_notes = notes;
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
// button. Everything stays in Approved, form open in Chrome, for a human to send.
window.triggerFill = function(jobId = null) {
    return triggerApply(jobId, false);
}

window.triggerApply = async function(jobId = null, submit = true) {
    const jobIds = jobId === null ? null : [].concat(jobId);
    const what = !jobIds ? "every approved job"
        : jobIds.length === 1 ? "this job" : `these ${jobIds.length} jobs`;
    const scope = submit ? `Apply for ${what} now?` : `Fill in the form for ${what}, without sending?`;
    const detail = submit
        ? "A browser will open the employer's form, fill it from your profile and SUBMIT it."
            + "\n\nIt will not submit if any required field is empty, any question went "
            + "unanswered, or a field refused input - those stay in 'Approved', form open, for you instead."
        : "A browser will open the employer's form and fill it from your profile, then stop at the "
            + "submit button.\n\nNothing is sent. The job stays in 'Approved', form open in Chrome, so you can "
            + "check it and submit yourself.";
    if (!confirm(scope + "\n\n" + detail + "\n\nEvery step is screenshotted.")) return;

    const btnApply = document.getElementById('run-apply-btn');
    const hoverBox = document.getElementById('pipeline-status-hover');
    if (btnApply) btnApply.disabled = true;
    if (hoverBox) hoverBox.classList.add('active');

    try {
        const body = jobIds ? { job_ids: jobIds, submit } : { submit };
        const res = await fetch('/api/apply', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });
        const data = await res.json();
        if (data.status === 'blocked') {
            alert(data.error);
            if (btnApply) btnApply.disabled = !window.applyPolicy.submit_allowed;
            if (hoverBox) hoverBox.classList.remove('active');
            return false;
        }
        if (data.status === 'already_running') {
            alert("An application run is already in progress.");
            return false;
        }
        if (jobIds) closeModal();
        pollScraperStatus();
        return true;
    } catch (e) {
        alert("Failed to start applications: " + e);
        if (btnApply) btnApply.disabled = !window.applyPolicy.submit_allowed;
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
                if (btnApply) btnApply.disabled = !window.applyPolicy.submit_allowed;
                btnStop.style.display = 'none';
                btnStop.disabled = false;
                btnStop.innerText = 'Stop';
                hoverBox.classList.remove('active');
                fetchJobs();
            } else {
                btnFull.disabled = false;
                btnEval.disabled = false;
                if (btnApply) btnApply.disabled = !window.applyPolicy.submit_allowed;
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
    if (submit) { submit.disabled = false; submit.innerText = 'Fetch & score'; }
    modal.classList.add('active');
    const input = document.getElementById('add-job-url');
    input.value = '';
    input.focus();
}

window.closeAddJob = function() {
    document.getElementById('add-job-modal').classList.remove('active');
}

// The dialog's own buttons step aside while it asks about a job already on file.
function showAddJobActions(show) {
    const actions = document.getElementById('add-job-actions');
    if (actions) actions.style.display = show ? 'flex' : 'none';
}

function addJobStatus(html, colour) {
    showAddJobActions(true);
    const el = document.getElementById('add-job-status');
    el.style.display = 'block';
    el.style.color = colour || 'var(--text-secondary)';
    el.innerHTML = html;
}

window.cancelAddJobChoice = function() {
    const el = document.getElementById('add-job-status');
    el.style.display = 'none';
    el.innerHTML = '';
    showAddJobActions(true);
}

window.submitAddJob = async function(confirmed = false) {
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
            body: JSON.stringify({ url: url, instructions: instructions, confirm: confirmed })
        });
        const data = await res.json();
        if (data.status === 'exists') {
            // Already on file: re-read and re-score it only if asked to.
            const j = data.job;
            const esc = t => String(t ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/'/g, '&#39;');
            const column = STATUS_NAMES[j.status] || j.status;
            const after = j.protected ? `It stays in ${esc(column)}.` : 'It will be put on To Do.';
            addJobStatus(
                `<strong>This job is already in the database:</strong><br>${esc(j.title || 'Role')} at `
                + `${esc(j.company || 'Unknown')} (#${esc(j.job_id)}, ${esc(column)}).<br><br>`
                + `Fetch its description again and re-evaluate it? ${after}<br><br>`
                + '<div class="add-job-choices">'
                + '<button class="btn btn-primary" onclick="submitAddJob(true)">Fetch again &amp; re-evaluate</button>'
                + `<button class="btn" onclick="closeAddJob(); openJobDetails('${esc(j.job_id)}')">Open the existing job</button>`
                + '<button class="btn" onclick="cancelAddJobChoice()">Cancel</button>'
                + '</div>', '#fbbf24');
            showAddJobActions(false);
            submit.disabled = false; submit.innerText = 'Fetch & score';
            return;
        }
        if (data.status === 'already_running') {
            addJobStatus('Another job is being added right now - try again in a moment.', '#fbbf24');
            submit.disabled = false; submit.innerText = 'Fetch & score';
            return;
        }
        if (data.status === 'error') {
            addJobStatus(data.error, '#f87171');
            submit.disabled = false; submit.innerText = 'Fetch & score';
            return;
        }
        pollAddJob();
    } catch (e) {
        addJobStatus('Could not start: ' + e, '#f87171');
        submit.disabled = false; submit.innerText = 'Fetch & score';
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
        submit.innerText = 'Fetch & score';

        if (!result) {
            addJobStatus('Finished, but no result was reported.', '#fbbf24');
        } else if (!result.ok) {
            addJobStatus(result.error, '#f87171');
        } else {
            const where = result.company_identified ? '' :
                '<br><span style="color:#fbbf24;">The employer could not be identified. Add an ' +
                'instruction like "the company is X" and try again.</span>';
            const scored = result.score != null
                ? `Scored <strong>${result.score}/10</strong> and put on the To Do board.`
                : 'It could not be scored, but it is on the To Do board.';
            addJobStatus(
                '<strong style="color:#4ade80;">Done.</strong> ' +
                `${result.title || 'Role'} at ${result.company || 'Unknown'}. ` +
                scored + ' Approve it to write the CV and cover letter.' + where +
                `<br><br><button class="btn btn-primary" onclick="closeAddJob(); openJobDetails('${result.job_id}')">` +
                'Open the job</button>', '#4ade80');
            fetchJobs();
        }
    } catch (e) {
        submit.disabled = false;
        submit.innerText = 'Fetch & score';
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
        window.settingsMode = data.mode;
        renderAlertSettings(data.alerts);
    }).catch(e => {
        status.style.display = 'block';
        status.style.color = '#f87171';
        status.innerText = 'Could not load the settings: ' + e;
    });
}

// Safe mode: LinkedIn is never searched, so the candidate's job alerts are the
// searches. Show what has been arriving, and what to create to match the file.
function renderAlertSettings(info) {
    const box = document.getElementById('settings-alerts');
    if (!info) { box.style.display = 'none'; box.innerHTML = ''; return; }
    const esc = t => String(t ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;');
    const recent = info.recent.length
        ? '<table style="width: 100%; font-size: 0.85rem; border-collapse: collapse;">' +
          '<tr style="text-align: left; opacity: 0.7;"><th>Alert</th><th>Emails</th><th>Jobs listed</th><th>New</th><th>At the cap</th></tr>' +
          info.recent.map(a => `<tr><td>${esc(a.alert)}</td><td>${a.emails}</td><td>${a.listed}</td><td>${a.added}</td>` +
              `<td style="color: ${a.capped ? '#fbbf24' : 'inherit'};">${a.capped}</td></tr>`).join('') + '</table>'
        : `<p style="opacity: 0.8;">${info.imap_configured
            ? 'No job-alert emails read in the last 14 days.'
            : 'IMAP is not configured, so no job-alert emails are read yet: set IMAP_HOST, IMAP_USER and IMAP_PASSWORD in .env.'}</p>`;
    const suggested = info.suggested.map(s =>
        `<li><code>${esc(s.keyword)}</code> in ${esc(s.location)}` +
        (s.wide ? ` <span style="color: #fbbf24;">- too broad for one alert; create one per country</span>` : '') +
        '</li>').join('');
    box.innerHTML = `
        <h4>Safe mode: your LinkedIn job alerts are the searches</h4>
        <p style="font-size: 0.9rem; line-height: 1.5;">LinkedIn is not searched in safe mode. New LinkedIn jobs come
            from the alerts you keep on LinkedIn (<em>Jobs &rarr; Job alerts</em>, daily, by email). Each email lists at most
            ${info.cap} jobs, so keep alerts narrow: an alert whose emails are often at the cap is missing jobs.</p>
        <h4 style="margin-top: 1rem;">Alerts seen in the last 14 days</h4>
        ${recent}
        <h4 style="margin-top: 1rem;">Alerts that match the searches below</h4>
        <ul style="font-size: 0.85rem; line-height: 1.6; padding-left: 20px;">${suggested}</ul>
        <p style="font-size: 0.85rem; opacity: 0.8;">The keywords below still drive the Adzuna search${info.adzuna_configured ? '' : ' (not configured)'}.</p>`;
    box.style.display = 'block';
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
                html += (window.settingsMode === 'safe'
                        ? `Adzuna will run ${p.searches} searches` : `The next run will make ${p.searches} searches (${p.pages} result pages)`) +
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
